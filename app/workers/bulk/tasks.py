"""Bulk operation worker tasks (bulk import, update, delete, export)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from geoalchemy2.elements import WKTElement
from shapely.geometry import shape as shapely_shape
from sqlalchemy import insert, select

from app.core.enums import JobStatus
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_set import AnnotationSet
from app.models.job import Job
from app.services import storage_service
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import BULK

logger = logging.getLogger(__name__)

# Annotations are inserted in chunks to keep transaction size bounded.
BATCH_SIZE = 1000

# Cap the number of per-feature error records we keep in the result summary
# (the full count is preserved separately).
MAX_ERROR_SAMPLE = 50


def _utcnow() -> datetime:
    return datetime.now(UTC)


@celery_app.task(bind=True, queue=BULK, max_retries=2, default_retry_delay=60)
def bulk_import_annotations(self, job_id: str) -> None:
    """Import annotations from a GeoJSON FeatureCollection stored in S3.

    All configuration is read from ``job.config``:

    ``annotation_set_id``  target set; must already exist with a schema
    ``s3_key``             object key in the org bucket
    ``default_class_id``   fallback class UUID for features whose class
                           cannot be resolved (optional)
    ``class_property``     property name on each feature carrying the class
                           UUID (default ``"class_id"``)
    ``confidence_property`` optional property name carrying a numeric
                            confidence value

    Each feature's geometry is validated, its class is resolved against the
    set's schema (with fallback to ``default_class_id``), and the row is
    inserted with ``created_by_job_id`` pointing at this job.  Features that
    fail validation or have an unresolvable class with no default are
    skipped and counted in ``failed_items``; the first ``MAX_ERROR_SAMPLE``
    error records are stored in ``job.config['result']['errors_sample']``
    for the UI to surface.
    """
    job_uuid = uuid.UUID(job_id)

    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("bulk_import_annotations: job %s not found", job_id)
            return

        cfg = dict(job.config or {})
        try:
            set_id = uuid.UUID(cfg["annotation_set_id"])
            s3_key = cfg["s3_key"]
        except (KeyError, ValueError, TypeError) as exc:
            job.status = JobStatus.FAILED
            job.logs = f"Invalid job config: {exc}"
            job.finished_at = _utcnow()
            session.commit()
            return

        default_class_id: uuid.UUID | None = None
        if cfg.get("default_class_id"):
            try:
                default_class_id = uuid.UUID(str(cfg["default_class_id"]))
            except (ValueError, TypeError):
                default_class_id = None

        class_property = cfg.get("class_property") or "class_id"
        confidence_property = cfg.get("confidence_property") or None

        job.status = JobStatus.RUNNING
        job.started_at = _utcnow()
        session.commit()

        try:
            annotation_set = session.get(AnnotationSet, set_id)
            if annotation_set is None or annotation_set.deleted_at is not None:
                raise ValueError(f"Annotation set {set_id} not found")
            if annotation_set.schema_id is None:
                raise ValueError(
                    "Annotation set has no schema_id; cannot resolve classes"
                )

            valid_classes: set[uuid.UUID] = {
                row[0]
                for row in session.execute(
                    select(AnnotationClass.id).where(
                        AnnotationClass.schema_id == annotation_set.schema_id
                    )
                )
            }
            if default_class_id is not None and default_class_id not in valid_classes:
                raise ValueError(
                    f"default_class_id {default_class_id} does not belong to "
                    f"the set's schema"
                )

            # ── Download GeoJSON from S3 ──
            client = storage_service._s3_client()
            bucket = storage_service.bucket_name(job.organization_id)
            obj = client.get_object(Bucket=bucket, Key=s3_key)
            raw = obj["Body"].read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not parse GeoJSON: {exc}") from exc

            if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
                raise ValueError("File is not a GeoJSON FeatureCollection")

            features = data.get("features")
            if not isinstance(features, list):
                raise ValueError("FeatureCollection has no features array")

            # CRS check — only EPSG:4326 / CRS84 / unspecified are supported.
            crs = ""
            crs_block = data.get("crs")
            if isinstance(crs_block, dict):
                crs = (crs_block.get("properties") or {}).get("name", "") or ""
            if crs and "CRS84" not in crs and "4326" not in crs:
                raise ValueError(
                    f"Unsupported CRS {crs!r}; only EPSG:4326 / CRS84 are "
                    "supported in v1"
                )

            total = len(features)
            job.total_items = total
            job.processed_items = 0
            job.failed_items = 0
            job.progress = 0.0
            session.commit()

            created = 0
            unmapped = 0
            errors: list[dict] = []
            error_total = 0
            batch: list[dict] = []

            for idx, feat in enumerate(features):
                try:
                    geom_dict = (feat or {}).get("geometry")
                    if geom_dict is None:
                        raise ValueError("missing geometry")
                    shp = shapely_shape(geom_dict)
                    if shp.is_empty:
                        raise ValueError("empty geometry")
                    if not shp.is_valid:
                        raise ValueError("invalid geometry")

                    props = (feat or {}).get("properties") or {}

                    raw_class = props.get(class_property)
                    resolved_class_id: uuid.UUID | None = None
                    if raw_class is not None:
                        try:
                            cid = uuid.UUID(str(raw_class))
                            if cid in valid_classes:
                                resolved_class_id = cid
                        except (ValueError, TypeError):
                            pass

                    if resolved_class_id is None:
                        if default_class_id is None:
                            raise ValueError(
                                f"unmapped class {raw_class!r} and no "
                                "default_class_id provided"
                            )
                        resolved_class_id = default_class_id
                        unmapped += 1

                    confidence: float | None = None
                    if confidence_property and confidence_property in props:
                        try:
                            confidence = float(props[confidence_property])
                        except (ValueError, TypeError):
                            confidence = None

                    extra_props = {
                        k: v
                        for k, v in props.items()
                        if k != class_property
                        and (confidence_property is None or k != confidence_property)
                    }

                    batch.append(
                        {
                            "annotation_set_id": set_id,
                            "class_id": resolved_class_id,
                            "geometry": WKTElement(shp.wkt, srid=4326),
                            "confidence": confidence,
                            "properties": extra_props or None,
                            "created_by_user_id": None,
                            "created_by_job_id": job_uuid,
                        }
                    )

                    if len(batch) >= BATCH_SIZE:
                        session.execute(insert(Annotation.__table__), batch)
                        created += len(batch)
                        batch = []
                        job.processed_items = idx + 1
                        job.progress = (idx + 1) / max(1, total)
                        session.commit()

                except Exception as exc:  # noqa: BLE001 — per-feature isolation
                    error_total += 1
                    if len(errors) < MAX_ERROR_SAMPLE:
                        errors.append({"index": idx, "error": str(exc)})

            if batch:
                session.execute(insert(Annotation.__table__), batch)
                created += len(batch)
                batch = []

            job.processed_items = total
            job.failed_items = error_total
            job.progress = 1.0
            cfg["result"] = {
                "created": created,
                "skipped": error_total,
                "unmapped_count": unmapped,
                "errors_total": error_total,
                "errors_sample": errors,
            }
            job.config = cfg
            job.status = JobStatus.COMPLETED
            job.finished_at = _utcnow()
            session.commit()

            logger.info(
                "bulk_import_annotations done job=%s set=%s created=%d "
                "skipped=%d unmapped=%d",
                job_id,
                set_id,
                created,
                error_total,
                unmapped,
            )

        except Exception as exc:
            session.rollback()
            logger.exception("bulk_import_annotations failed job=%s", job_id)
            try:
                job = session.get(Job, job_uuid)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.logs = str(exc)[:4000]
                    job.finished_at = _utcnow()
                    session.commit()
            except Exception:
                logger.exception("could not mark job %s as failed", job_id)
            raise


def _gdal_env_for_worker() -> dict:
    """rasterio Env options; credentials come from the worker env via boto3."""
    from app.config import settings

    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if settings.AWS_ENDPOINT_URL.startswith("https://") else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }


@celery_app.task(bind=True, queue=BULK, max_retries=2, default_retry_delay=60)
def vectorize_raster_mask(self, job_id: str) -> None:
    """Vectorize a raster-mask annotation set into a new vector set (async).

    Dispatched onto the Redis-backed ``bulk`` queue. Reads ``job.config``:

    ``source_set_id``     raster-mask annotation set to vectorize (required)
    ``simplify_tolerance``/``min_area_px``/``connectivity``/``dissolve``/
    ``confidence``/``out_name``  optional conversion knobs

    On success, ``job.config['result']`` carries the new set id + class counts.
    """
    from app.services.conversion import vectorize_raster_mask_set

    job_uuid = uuid.UUID(job_id)
    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("vectorize_raster_mask: job %s not found", job_id)
            return
        cfg = dict(job.config or {})
        try:
            job.status = JobStatus.RUNNING
            job.started_at = _utcnow()
            session.commit()

            source_set_id = cfg.get("source_set_id")
            if not source_set_id:
                raise ValueError("job.config.source_set_id is required")
            raster_set = session.get(AnnotationSet, uuid.UUID(str(source_set_id)))
            if raster_set is None or raster_set.organization_id != job.organization_id:
                raise ValueError("source annotation set not found in this organization")

            result = vectorize_raster_mask_set(
                session,
                raster_set,
                gdal_env=_gdal_env_for_worker(),
                name=cfg.get("out_name"),
                created_by_user_id=job.created_by_user_id,
                simplify_tolerance=cfg.get("simplify_tolerance"),
                min_area_px=float(cfg.get("min_area_px", 0.0)),
                connectivity=int(cfg.get("connectivity", 4)),
                dissolve_by_class=bool(cfg.get("dissolve", False)),
                confidence=cfg.get("confidence", 1.0),
            )

            cfg["result"] = {
                "annotation_set_id": str(result.annotation_set_id),
                "feature_count": result.feature_count,
                "class_counts": result.class_counts,
            }
            job.config = cfg
            job.processed_items = result.feature_count
            job.total_items = result.feature_count
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            job.finished_at = _utcnow()
            session.commit()
            logger.info(
                "vectorize_raster_mask done job=%s source=%s target=%s features=%d",
                job_id, source_set_id, result.annotation_set_id, result.feature_count,
            )
        except Exception as exc:
            session.rollback()
            logger.exception("vectorize_raster_mask failed job=%s", job_id)
            try:
                job = session.get(Job, job_uuid)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.logs = str(exc)[:4000]
                    job.finished_at = _utcnow()
                    session.commit()
            except Exception:
                logger.exception("could not mark job %s as failed", job_id)
            raise


__all__ = ["bulk_import_annotations", "vectorize_raster_mask", "BULK"]
