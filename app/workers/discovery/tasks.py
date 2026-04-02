"""Discovery worker tasks.

Queue: discovery
Implemented in Steps 7 (dataset relationships) and 12a (object tracking auto-match).
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models.annotation import Annotation
from app.models.dataset import Dataset, DatasetRelationship
from app.models.job import Job
from app.models.tracking import TrackedObject, TrackedObjectObservation
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import DISCOVERY

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue=DISCOVERY, max_retries=3, default_retry_delay=60)
def discover_dataset_relationships(self, job_id: str, dataset_id: str) -> None:
    """Find spatial/temporal relationships between a newly ingested dataset and existing ones.

    Relationship types (from CLAUDE.md):
      same_area_different_sensor  — spatial overlap, overlapping time periods
      temporal_continuation       — spatial overlap, sequential non-overlapping time periods
      supplements                 — spatial overlap, no strong temporal match

    Inserts DatasetRelationship rows (on-conflict-do-nothing — idempotent).

    Args:
        job_id:     UUID string of a pre-created Job row (may be None / missing — handled).
        dataset_id: UUID string of the newly ingested Dataset.
    """
    with WorkerSession() as session:
        job: Job | None = session.get(Job, uuid.UUID(job_id)) if job_id else None
        if job:
            job.status = "running"
            job.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            job.celery_task_id = self.request.id
            session.commit()

        try:
            _run_discovery(session, uuid.UUID(dataset_id))
            if job:
                job.status = "completed"
                job.progress = 1.0
                job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                session.commit()
        except Exception as exc:
            logger.exception("discover_dataset_relationships failed for %s", dataset_id)
            if job:
                try:
                    job.status = "failed"
                    job.error = str(exc)[:1000]
                    session.commit()
                except Exception:
                    session.rollback()
            raise self.retry(exc=exc)


def _run_discovery(session, dataset_id: uuid.UUID) -> None:
    """Core relationship discovery logic.

    Finds existing datasets in the same organisation whose spatial_extent
    intersects the new dataset.  Determines the relationship type based on
    temporal extent overlap, then inserts DatasetRelationship rows.
    """
    new_ds: Dataset | None = session.get(Dataset, dataset_id)
    if new_ds is None or new_ds.spatial_extent is None:
        logger.info("discover_dataset_relationships: dataset %s has no spatial extent — skipping", dataset_id)
        return

    # Find candidate datasets in the same org that have a spatial extent,
    # excluding the new dataset itself and any that are not 'active'.
    sql = text("""
        SELECT id, temporal_extent_start, temporal_extent_end
        FROM datasets
        WHERE organization_id = :org_id
          AND id <> :dataset_id
          AND status = 'active'
          AND spatial_extent IS NOT NULL
          AND ST_Intersects(spatial_extent, ST_GeomFromEWKT(:geom))
    """)
    rows = session.execute(sql, {
        "org_id": str(new_ds.organization_id),
        "dataset_id": str(dataset_id),
        "geom": f"SRID=4326;{_wkb_to_wkt(new_ds.spatial_extent)}",
    }).fetchall()

    inserted = 0
    for row in rows:
        candidate_id: uuid.UUID = row[0]
        candidate_start: datetime | None = row[1]
        candidate_end: datetime | None = row[2]

        rel_type = _determine_relationship_type(
            new_ds.temporal_extent_start,
            new_ds.temporal_extent_end,
            candidate_start,
            candidate_end,
        )

        # Insert both directions (source→target AND target→source) so that
        # queries from either side return the relationship.
        for src, tgt in [(dataset_id, candidate_id), (candidate_id, dataset_id)]:
            try:
                existing = session.execute(
                    select(DatasetRelationship).where(
                        DatasetRelationship.source_dataset_id == src,
                        DatasetRelationship.target_dataset_id == tgt,
                        DatasetRelationship.relationship_type == rel_type,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    rel = DatasetRelationship(
                        organization_id=new_ds.organization_id,
                        source_dataset_id=src,
                        target_dataset_id=tgt,
                        relationship_type=rel_type,
                    )
                    session.add(rel)
                    inserted += 1
            except Exception as exc:
                logger.warning("Could not insert relationship %s→%s: %s", src, tgt, exc)

    session.commit()
    logger.info(
        "discover_dataset_relationships: dataset=%s candidates=%d relationships_inserted=%d",
        dataset_id, len(rows), inserted,
    )


def _determine_relationship_type(
    new_start: datetime | None,
    new_end: datetime | None,
    cand_start: datetime | None,
    cand_end: datetime | None,
) -> str:
    """Choose a relationship type based on temporal extent comparison.

    - ``same_area_different_sensor``: temporal ranges overlap (or both missing).
    - ``temporal_continuation``: one dataset follows the other sequentially.
    - ``supplements``: no clear temporal pattern.
    """
    if new_start is None or new_end is None or cand_start is None or cand_end is None:
        # Cannot determine temporal relationship — default to 'supplements'
        return "same_area_different_sensor"

    # Overlap: ranges intersect
    if new_start <= cand_end and cand_start <= new_end:
        return "same_area_different_sensor"

    # Sequential: one period immediately follows the other (within 30 days gap)
    from datetime import timedelta
    gap_threshold = timedelta(days=30)
    if abs((new_start - cand_end).days) <= gap_threshold.days:
        return "temporal_continuation"
    if abs((cand_start - new_end).days) <= gap_threshold.days:
        return "temporal_continuation"

    return "supplements"


def _wkb_to_wkt(wkb_element) -> str:
    """Convert a geoalchemy2 WKBElement to WKT for use in a raw SQL string."""
    from geoalchemy2.shape import to_shape
    return to_shape(wkb_element).wkt


@celery_app.task(bind=True, queue=DISCOVERY, max_retries=3, default_retry_delay=60)
def auto_match_tracked_objects(self, job_id: str, annotation_id: str, org_id: str) -> None:
    """Spatially match a new annotation to existing tracked objects or create a new one.

    Algorithm (from CLAUDE.md):
      1. Find all active tracked objects of the same type within max_distance_m and
         last observed within max_gap_days.
      2. If any match: link annotation.track_id to the closest object and add an
         observation row.
      3. If no match: create a new TrackedObject and the first observation.
    """
    MAX_DISTANCE_M = 5_000.0  # 5 km
    MAX_GAP_DAYS = 90

    _VALID_OBJECT_TYPES = {
        "deforestation_front", "fire_perimeter", "building", "water_body", "custom",
    }

    with WorkerSession() as session:
        try:
            ann = session.get(Annotation, uuid.UUID(annotation_id))
            if ann is None or ann.geometry is None:
                logger.info(
                    "auto_match_tracked_objects: annotation %s missing or has no geometry — skip",
                    annotation_id,
                )
                return

            # Convert geometry to WKT for raw SQL
            from geoalchemy2.shape import to_shape
            geom_wkt = to_shape(ann.geometry).wkt

            from datetime import timedelta
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=MAX_GAP_DAYS)

            # Spatial + temporal proximity query (CLAUDE.md spec)
            rows = session.execute(
                text("""
                    SELECT id,
                           ST_Distance(
                               latest_geometry::geography,
                               ST_GeomFromText(:geom_wkt, 4326)::geography
                           ) AS dist
                    FROM tracked_objects
                    WHERE organization_id = :org_id
                      AND status = 'active'
                      AND object_type = :label
                      AND latest_geometry IS NOT NULL
                      AND ST_DWithin(
                              latest_geometry::geography,
                              ST_GeomFromText(:geom_wkt, 4326)::geography,
                              :max_dist
                          )
                      AND last_observed_at > :cutoff
                    ORDER BY dist ASC
                    LIMIT 5
                """),
                {
                    "geom_wkt": geom_wkt,
                    "org_id": str(uuid.UUID(org_id)),
                    "label": ann.label,
                    "max_dist": MAX_DISTANCE_M,
                    "cutoff": cutoff,
                },
            ).fetchall()

            obj_type = ann.label if ann.label in _VALID_OBJECT_TYPES else "custom"
            obs_dt = _get_annotation_datetime(session, ann)

            if rows:
                # Best match: closest
                best_id = rows[0][0]
                tracked_obj = session.get(TrackedObject, best_id)
                if tracked_obj:
                    ann.track_id = tracked_obj.id
                    _add_observation_sync(session, tracked_obj, ann, obs_dt)
                    session.commit()
                    logger.info(
                        "auto_match: annotation %s linked to existing tracked_object %s",
                        annotation_id, best_id,
                    )
                    return

            # No match — create a new TrackedObject
            tracked_obj = TrackedObject(
                organization_id=uuid.UUID(org_id),
                object_type=obj_type,
                status="active",
                priority="medium",
                severity="info",
            )
            session.add(tracked_obj)
            session.flush()

            _add_observation_sync(session, tracked_obj, ann, obs_dt)
            ann.track_id = tracked_obj.id
            session.commit()
            logger.info(
                "auto_match: annotation %s created new tracked_object %s",
                annotation_id, tracked_obj.id,
            )

        except Exception as exc:
            logger.exception(
                "auto_match_tracked_objects failed for annotation %s", annotation_id
            )
            try:
                session.rollback()
            except Exception:
                pass
            raise self.retry(exc=exc)


def _get_annotation_datetime(session, ann) -> datetime:
    """Return the dataset item datetime for the annotation, falling back to now."""
    from app.models.dataset import DatasetItem
    item = session.get(DatasetItem, ann.dataset_item_id)
    if item and item.datetime:
        dt = item.datetime
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_observation_sync(session, tracked_obj: TrackedObject, ann, obs_dt: datetime) -> None:
    """Create a TrackedObjectObservation and update tracked object aggregates (sync)."""
    obs = TrackedObjectObservation(
        tracked_object_id=tracked_obj.id,
        annotation_id=ann.id,
        observation_datetime=obs_dt,
        geometry=ann.geometry,
        properties={"source": "auto_match"},
    )
    session.add(obs)
    session.flush()
    _update_aggregates_sync(session, tracked_obj)


def _update_aggregates_sync(session, tracked_obj: TrackedObject) -> None:
    """Sync version of _update_aggregates for use in Celery workers."""
    obj_id = str(tracked_obj.id)

    row = session.execute(
        text(
            "SELECT COUNT(*) AS cnt, "
            "MIN(observation_datetime) AS first_obs, "
            "MAX(observation_datetime) AS last_obs "
            "FROM tracked_object_observations "
            "WHERE tracked_object_id = :obj_id"
        ),
        {"obj_id": obj_id},
    ).fetchone()
    if row:
        tracked_obj.observation_count = row.cnt
        tracked_obj.first_observed_at = row.first_obs
        tracked_obj.last_observed_at = row.last_obs

    geom_row = session.execute(
        text(
            "SELECT geometry FROM tracked_object_observations "
            "WHERE tracked_object_id = :obj_id AND geometry IS NOT NULL "
            "ORDER BY observation_datetime DESC LIMIT 1"
        ),
        {"obj_id": obj_id},
    ).fetchone()
    if geom_row and geom_row[0] is not None:
        tracked_obj.latest_geometry = geom_row[0]

    cum_row = session.execute(
        text(
            "SELECT ST_Union(geometry) FROM tracked_object_observations "
            "WHERE tracked_object_id = :obj_id AND geometry IS NOT NULL"
        ),
        {"obj_id": obj_id},
    ).fetchone()
    if cum_row and cum_row[0] is not None:
        tracked_obj.cumulative_geometry = cum_row[0]

    session.flush()
