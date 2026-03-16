"""
Celery ingestion task: validate, register in pgSTAC, and populate Dataset.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import UTC, datetime

from celery import shared_task
from geoalchemy2 import WKTElement
from psycopg2.extras import DateTimeTZRange
from shapely.geometry import box
from shapely import wkt as shapely_wkt
from sqlalchemy import select, func

from app.config import settings
from app.core.enums import DatasetStatus, JobStatus
from app.models.dataset import Dataset
from app.models.job import Job
from app.services import storage_service
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import INGESTION
from app.workers.ingestion.pypgstac_utils import upsert_stac_collection, upsert_stac_item
from app.workers.ingestion.rasterio_utils import (
    build_stac_collection,
    build_stac_item,
    extract_cog_metadata,
    validate_cog,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _s3_config_for_worker() -> dict:
    """Build the GDAL environment dict for VSI S3 access to MinIO."""
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_ACCESS_KEY_ID": settings.AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": settings.AWS_SECRET_ACCESS_KEY,
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }


def _extend_temporal_extent(
    existing: DateTimeTZRange | None,
    item_dt: datetime,
) -> DateTimeTZRange:
    """Return a TSTZRANGE that covers both *existing* and *item_dt*."""
    if existing is None:
        return DateTimeTZRange(item_dt, item_dt, bounds="[]")
    lower = min(existing.lower, item_dt) if existing.lower else item_dt
    upper = max(existing.upper, item_dt) if existing.upper else item_dt
    return DateTimeTZRange(lower, upper, bounds="[]")


def _extend_spatial_extent(
    session,
    dataset: Dataset,
    new_bbox: list[float],
) -> WKTElement:
    """Return a POLYGON WKTElement that covers existing geometry + *new_bbox*.

    Always returns a POLYGON (MBR) so the column type constraint is satisfied,
    even when the union of two non-overlapping geometries would be MULTIPOLYGON.
    """
    west, south, east, north = new_bbox
    new_shape = box(west, south, east, north)

    if dataset.geometry is not None:
        existing_wkt = session.execute(
            select(func.ST_AsText(Dataset.geometry)).where(Dataset.id == dataset.id)
        ).scalar()
        if existing_wkt:
            existing_shape = shapely_wkt.loads(existing_wkt)
            # Use envelope (MBR) to guarantee POLYGON output
            combined = existing_shape.union(new_shape).envelope
            return WKTElement(combined.wkt, srid=4326)

    return WKTElement(new_shape.wkt, srid=4326)


@celery_app.task(
    bind=True,
    queue=INGESTION,
    max_retries=3,
    default_retry_delay=60,
)
def ingest_dataset(
    self,
    job_id: str,
    dataset_id: str,
    s3_key: str,
    filename: str,
) -> None:
    """Validate a COG, register it in pgSTAC, and populate the Dataset record.

    Task lifecycle:
        pending (created by API)
        → running  (set here on entry)
        → completed / failed
    """
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.error("ingest_dataset job_not_found job_id=%s", job_id)
            return

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        # Mark dataset as ingesting so the API can surface this state
        dataset_early = session.get(Dataset, uuid.UUID(dataset_id))
        if dataset_early is not None:
            dataset_early.status = DatasetStatus.INGESTING
        session.commit()

        try:
            bucket = storage_service.bucket_name(job.organization_id)
            s3_uri = f"s3://{bucket}/{s3_key}"
            s3_config = _s3_config_for_worker()

            # ── 1. Validate COG ───────────────────────────────────────────────
            is_valid, issues = validate_cog(s3_uri, s3_config)
            if not is_valid:
                job.status = JobStatus.FAILED
                job.logs = "COG validation failed:\n" + "\n".join(issues)
                job.finished_at = _now()
                if dataset_early is not None:
                    dataset_early.status = DatasetStatus.FAILED
                session.commit()
                logger.warning(
                    "ingest_dataset_cog_invalid job_id=%s issues=%s", job_id, issues
                )
                return

            # ── 2. Extract metadata ───────────────────────────────────────────
            metadata = extract_cog_metadata(s3_uri, s3_config)
            metadata["filename"] = filename

            # ── 3. Upsert STAC Collection ─────────────────────────────────────
            collection_id = f"org-{job.organization_id}-dataset-{dataset_id}"
            dataset = dataset_early
            if dataset is None:
                raise ValueError(f"Dataset {dataset_id} not found")

            collection_name = dataset.name
            collection = build_stac_collection(
                collection_id, str(job.organization_id), collection_name
            )
            upsert_stac_collection(collection, settings.STAC_SYNC_DATABASE_URL)

            # ── 4. Upsert STAC Item ───────────────────────────────────────────
            item_id = str(uuid.uuid4())
            item = build_stac_item(item_id, collection_id, s3_uri, metadata)
            upsert_stac_item(item, settings.STAC_SYNC_DATABASE_URL)

            # ── 5. Update Dataset ─────────────────────────────────────────────
            dataset.stac_collection_id = collection_id

            # Spatial extent — union with existing, always stored as POLYGON MBR
            dataset.geometry = _extend_spatial_extent(session, dataset, metadata["bbox"])

            # Temporal extent — extend to cover new item datetime
            item_dt_raw = metadata["datetime"]  # ISO-8601 string
            item_dt = datetime.fromisoformat(item_dt_raw.replace("Z", "+00:00"))
            item_dt_naive = item_dt.replace(tzinfo=None)  # DB stores timezone-naive UTC
            dataset.temporal_extent = _extend_temporal_extent(
                dataset.temporal_extent, item_dt_naive
            )

            # Metadata — overwrite with latest file's properties
            dataset.metadata_ = {
                "native_crs": metadata.get("native_crs"),
                "gsd_meters": metadata.get("gsd_meters"),
                "bands": metadata.get("bands"),
                "width": metadata.get("width"),
                "height": metadata.get("height"),
                "file_size_bytes": metadata.get("file_size_bytes"),
            }

            dataset.status = DatasetStatus.READY
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()

            logger.info(
                "ingest_dataset_success job_id=%s dataset_id=%s stac_item_id=%s",
                job_id,
                dataset_id,
                item_id,
            )

        except Exception as exc:
            session.rollback()
            job.status = JobStatus.FAILED
            job.logs = traceback.format_exc()
            job.finished_at = _now()
            # Re-fetch dataset after rollback so the status update lands cleanly
            _ds = session.get(Dataset, uuid.UUID(dataset_id))
            if _ds is not None:
                _ds.status = DatasetStatus.FAILED
            session.commit()
            logger.error("ingest_dataset_failed job_id=%s error=%s", job_id, exc)
            raise self.retry(exc=exc)
