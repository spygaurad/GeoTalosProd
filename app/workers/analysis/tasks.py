"""Analysis worker tasks (analysis queue)."""
import logging
import uuid
from datetime import UTC, datetime

from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import ANALYSIS

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue=ANALYSIS, max_retries=2, default_retry_delay=60)
def run_change_detection_job(self, job_id: str) -> None:
    """
    Raster-based change detection. Creates new annotations for changed areas
    by running PostGIS ST_Difference, then resumes the automation pipeline.

    Called when change_detection node uses detection_method='raster_threshold'.
    """
    with WorkerSession() as session:
        from app.models.job import Job
        from app.models.annotation_set import AnnotationSet
        from sqlalchemy import text

        try:
            job = session.get(Job, uuid.UUID(job_id))
            if not job:
                logger.warning("run_change_detection_job: job %s not found", job_id)
                return

            job.status = "running"
            job.started_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

            # Load config
            cfg = job.config or {}
            before_set_id = cfg.get("before_annotation_set_id")
            after_set_id = cfg.get("after_annotation_set_id")
            threshold = cfg.get("threshold", 0.1)
            min_change_area_sqm = cfg.get("min_change_area_sqm", 10)

            if not before_set_id or not after_set_id:
                raise ValueError("before_annotation_set_id and after_annotation_set_id required")

            before_uuid = uuid.UUID(before_set_id)
            after_uuid = uuid.UUID(after_set_id)

            # Create output annotation set for changed areas
            output_set = AnnotationSet(
                name=f"Change Detection Results ({job_id[:8]})",
                created_by_job_id=job.id,
            )
            session.add(output_set)
            session.flush()

            # Run PostGIS change detection query
            # Find areas in 'after' that differ from 'before' by more than threshold
            result = session.execute(
                text("""
                    INSERT INTO annotations (
                        annotation_set_id, geometry, confidence, created_by_job_id
                    )
                    SELECT
                        :output_set_id,
                        ST_Difference(
                            a.geometry,
                            COALESCE(
                                (SELECT ST_Union(b.geometry)
                                 FROM annotations b
                                 WHERE b.annotation_set_id = :before_set_id
                                   AND b.deleted_at IS NULL
                                   AND ST_Intersects(a.geometry, b.geometry)
                                ),
                                'SRID=4326;GEOMETRYCOLLECTION EMPTY'::geometry
                            )
                        ) AS changed_geom,
                        a.confidence,
                        :job_id
                    FROM annotations a
                    WHERE a.annotation_set_id = :after_set_id
                      AND a.deleted_at IS NULL
                      AND ST_Area(
                        ST_Difference(
                            a.geometry,
                            COALESCE(
                                (SELECT ST_Union(b.geometry)
                                 FROM annotations b
                                 WHERE b.annotation_set_id = :before_set_id
                                   AND b.deleted_at IS NULL
                                   AND ST_Intersects(a.geometry, b.geometry)
                                ),
                                'SRID=4326;GEOMETRYCOLLECTION EMPTY'::geometry
                            )
                        )::geography
                      ) > :min_area
                    RETURNING id
                """),
                {
                    "output_set_id": output_set.id,
                    "before_set_id": before_uuid,
                    "after_set_id": after_uuid,
                    "job_id": job.id,
                    "min_area": min_change_area_sqm,
                }
            )
            change_count = result.rowcount or 0

            session.flush()

            job.status = "completed"
            job.progress = 1.0
            job.finished_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

            # Resume the automation pipeline step that was waiting on this job
            automation_run_id = cfg.get("automation_run_id")
            automation_step_id = cfg.get("automation_step_id")

            if automation_run_id and automation_step_id:
                from app.workers.automation.tasks import resume_after_job

                output_data = {
                    "changed_areas": {
                        "id": str(output_set.id),
                        "name": output_set.name,
                        "count": change_count,
                    },
                    "change_metrics": {
                        "method": "raster_threshold",
                        "threshold": threshold,
                        "min_change_area_sqm": min_change_area_sqm,
                        "total_changes": change_count,
                    },
                }
                resume_after_job.delay(job_id, output_data)

            logger.info("run_change_detection_job %s completed: %d changes detected", job_id, change_count)

        except Exception as exc:
            logger.exception("run_change_detection_job %s failed", job_id)
            with WorkerSession() as session2:
                job = session2.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "failed"
                    job.logs = str(exc)[:2000]
                    job.finished_at = datetime.now(UTC).replace(tzinfo=None)
                    session2.commit()
            raise self.retry(exc=exc)
