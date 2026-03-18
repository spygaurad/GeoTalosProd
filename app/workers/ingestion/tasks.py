"""
Celery ingestion task: validate, register in pgSTAC, and populate Dataset.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime

import boto3
from botocore.client import Config
from geoalchemy2 import WKTElement
from psycopg2.extras import DateTimeTZRange
from sqlalchemy import create_engine, text

from app.config import settings
from app.core.enums import DatasetStatus, JobStatus
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
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

# --- Configuration & Constants ---
MAX_ZIP_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024  # 10GB

# Lazily-initialised engine — created only inside the Celery worker process,
# not when the API imports this module to call apply_async().
_pgstac_engine = None


def _get_pgstac_engine():
    global _pgstac_engine
    if _pgstac_engine is None:
        _pgstac_engine = create_engine(
            settings.STAC_SYNC_DATABASE_URL,
            pool_size=5,
            max_overflow=10,
        )
    return _pgstac_engine

class PermanentTaskError(Exception):
    """Errors that should NOT trigger celery retry."""
    pass

def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

def _gdal_env_for_worker() -> dict:
    """GDAL config options safe to pass to rasterio.Env().

    rasterio 1.4+ raises EnvError if AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY
    are passed directly — credentials must come from boto3's credential chain.
    The worker container already has both keys in its environment (injected by
    docker-compose x-app-env), so boto3 picks them up automatically.
    Only the non-credential endpoint/path-style options are passed here.
    """
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }

def _deterministic_item_id(s3_uri: str) -> str:
    return hashlib.md5(s3_uri.encode()).hexdigest()

def _file_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()

# --- Core Logic ---

def _compute_aggregated_metadata(collection: str) -> dict | None:
    """
    Query pgSTAC once to get all aggregated metadata, spatial extent,
    and temporal range. This is the source of truth.
    """
    # pgSTAC stores the full STAC item JSON in `content`. Properties are at
    # content->'properties'. The `datetime` and `geometry` columns are extracted
    # separately for indexing and can be used directly.
    query = text("""
        SELECT
            (SELECT array_agg(DISTINCT b)
             FROM pgstac.items i2,
                  jsonb_array_elements_text(i2.content->'properties'->'bands') b
             WHERE i2.collection = :cid) AS bands,
            min((content->'properties'->>'gsd')::float) AS gsd_min,
            max((content->'properties'->>'gsd')::float) AS gsd_max,
            count(*) AS file_count,
            sum((content->'properties'->>'file_size_bytes')::bigint) AS total_size,
            array_agg(DISTINCT content->'properties'->>'native_crs') AS native_crs,
            ST_AsText(ST_Envelope(ST_Collect(geometry))) AS combined_wkt,
            min(datetime) AS start_date,
            max(datetime) AS end_date
        FROM pgstac.items
        WHERE collection = :cid
    """)

    with _get_pgstac_engine().connect() as conn:
        result = conn.execute(query, {"cid": collection}).mappings().first()
        
    if not result or result["file_count"] == 0:
        return None

    return {
        "metadata": {
            "band_count": result["bands"] or [],
            "gsd_min": result["gsd_min"],
            "gsd_max": result["gsd_max"],
            "file_count": result["file_count"],
            "total_size_bytes": result["total_size"] or 0,
            "native_crs": result["native_crs"] or [],
        },
        "wkt": result["combined_wkt"],
        "start_date": result["start_date"],
        "end_date": result["end_date"],
    }

def _ensure_collection(session, dataset: Dataset, org_id: str) -> str:
    if dataset.stac_collection_id:
        return dataset.stac_collection_id

    cid = f"org-{org_id}-dataset-{dataset.id}"
    collection_doc = build_stac_collection(cid, str(org_id), dataset.name)
    upsert_stac_collection(collection_doc, settings.STAC_SYNC_DATABASE_URL)

    dataset.stac_collection_id = cid
    session.commit()
    return cid

def _ingest_single_cog(
    s3_uri: str,
    filename: str,
    collection: str,
    s3_config: dict,
) -> tuple[bool, list[str], str | None, dict | None]:
    """Validate a COG and insert it as a STAC item.

    Returns (success, issues, stac_item_id, stac_item_dict).
    On failure stac_item_id and stac_item_dict are None.
    """
    is_valid, issues = validate_cog(s3_uri, s3_config)
    if not is_valid:
        return False, issues, None, None
    if issues:
        logger.warning("cog_warnings uri=%s warnings=%s", s3_uri, issues)

    metadata = extract_cog_metadata(s3_uri, s3_config)
    metadata["filename"] = filename
    item_id = _deterministic_item_id(s3_uri)

    item = build_stac_item(item_id, collection, s3_uri, metadata)
    upsert_stac_item(item, settings.STAC_SYNC_DATABASE_URL)

    return True, issues, item_id, item


def _upsert_dataset_item(
    session,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    stac_item_id: str,
    stac_collection_id: str,
    s3_uri: str,
    filename: str,
    stac_item: dict,
) -> None:
    """Upsert a DatasetItem row after a successful COG ingestion.

    Uses SQLAlchemy merge-by-unique-key pattern: load existing row if present,
    otherwise create a new one.  Idempotent — safe to re-run on retry.
    """
    from sqlalchemy import select as sync_select  # noqa: PLC0415

    existing = session.execute(
        sync_select(DatasetItem).where(DatasetItem.stac_item_id == stac_item_id)
    ).scalar_one_or_none()

    # Extract geometry (GeoJSON dict) and datetime from the STAC item
    geometry = stac_item.get("geometry")
    dt_str = (stac_item.get("properties") or {}).get("datetime")
    item_datetime = None
    if dt_str:
        from datetime import timezone  # noqa: PLC0415
        from dateutil import parser as dtparser  # noqa: PLC0415
        try:
            item_datetime = dtparser.parse(dt_str).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    properties_cache = stac_item.get("properties")

    if existing is None:
        row = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset_id,
            organization_id=organization_id,
            stac_item_id=stac_item_id,
            stac_collection_id=stac_collection_id,
            s3_uri=s3_uri,
            filename=filename,
            geometry=geometry,
            item_datetime=item_datetime,
            properties_cache=properties_cache,
            is_active=True,
        )
        session.add(row)
    else:
        # Idempotent update — keep the row current
        existing.is_active = True
        existing.s3_uri = s3_uri
        existing.filename = filename
        existing.geometry = geometry
        existing.item_datetime = item_datetime
        existing.properties_cache = properties_cache
    
def _ingest_zip(session, job, dataset, bucket, s3_key, dataset_id, gdal_env):
    collection = _ensure_collection(session, dataset, job.organization_id)

    boto_client = boto3.client(
        "s3",
        endpoint_url=settings.AWS_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        config=Config(s3={"addressing_style": "path"}),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "upload.zip")
        logger.info("Downloading ZIP s3://%s/%s", bucket, s3_key)
        boto_client.download_file(bucket, s3_key, zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise PermanentTaskError(f"ZIP exceeds limit of {MAX_ZIP_UNCOMPRESSED_BYTES} bytes")

            tif_members = [m for m in zf.namelist() if m.lower().endswith((".tif", ".tiff"))]
            if not tif_members:
                raise PermanentTaskError(f"ZIP {s3_key} contains no .tif files")

            total = len(tif_members)
            job.total_items = total
            job.processed_items = 0
            session.commit()

            failed_files = []
            extract_base = os.path.join(tmpdir, "extracted")

            for idx, member in enumerate(tif_members):
                file_path = zf.extract(member, extract_base)
                basename = os.path.basename(file_path)

                try:
                    file_hash = _file_hash(file_path)
                    unique_name = f"{file_hash}_{basename}"
                    extracted_key = f"datasets/{dataset_id}/{unique_name}"
                    s3_uri = f"s3://{bucket}/{extracted_key}"

                    storage_service.upload_from_path(
                        job.organization_id, extracted_key, file_path
                    )

                    success, issues, item_id, stac_item = _ingest_single_cog(
                        s3_uri, basename, collection, gdal_env
                    )

                    if success:
                        job.processed_items += 1
                        _upsert_dataset_item(
                            session,
                            dataset_id=uuid.UUID(dataset_id),
                            organization_id=job.organization_id,
                            stac_item_id=item_id,
                            stac_collection_id=collection,
                            s3_uri=s3_uri,
                            filename=basename,
                            stac_item=stac_item,
                        )
                    else:
                        failed_files.append(f"{basename}: {'; '.join(issues)}")

                finally:
                    if os.path.exists(file_path):
                        os.remove(file_path)

                job.failed_items = len(failed_files)
                job.progress = (idx + 1) / total
                if (idx + 1) % 10 == 0 or (idx + 1) == total:
                    session.commit()

            if job.processed_items == 0:
                raise PermanentTaskError(f"All files failed validation: {failed_files}")

            if failed_files:
                job.logs = "Partial success – some files skipped:\n" + "\n".join(failed_files)

            
@celery_app.task(bind=True, queue=INGESTION, max_retries=3, default_retry_delay=60)
def ingest_dataset(self, job_id: str, dataset_id: str, s3_key: str, filename: str):
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        dataset = session.get(Dataset, uuid.UUID(dataset_id))

        if not job or not dataset:
            logger.error("Job %s or Dataset %s missing", job_id, dataset_id)
            return

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        dataset.status = DatasetStatus.INGESTING
        session.commit()

        try:
            bucket = storage_service.bucket_name(job.organization_id)
            gdal_env = _gdal_env_for_worker()

            if filename.lower().endswith(".zip"):
                _ingest_zip(session, job, dataset, bucket, s3_key, dataset_id, gdal_env)
            else:
                collection = _ensure_collection(session, dataset, job.organization_id)
                s3_uri = f"s3://{bucket}/{s3_key}"
                success, issues, item_id, stac_item = _ingest_single_cog(
                    s3_uri, filename, collection, gdal_env
                )

                if not success:
                    raise PermanentTaskError("\n".join(issues))

                _upsert_dataset_item(
                    session,
                    dataset_id=uuid.UUID(dataset_id),
                    organization_id=job.organization_id,
                    stac_item_id=item_id,
                    stac_collection_id=collection,
                    s3_uri=s3_uri,
                    filename=filename,
                    stac_item=stac_item,
                )
                job.processed_items = 1
                job.progress = 1.0
                session.commit()

            # --- Final Idempotent Aggregation ---
            agg = _compute_aggregated_metadata(dataset.stac_collection_id)
            if agg is None:
                raise PermanentTaskError(
                    f"Ingestion pipeline completed but no items were found in pgSTAC for "
                    f"collection {dataset.stac_collection_id!r}. "
                    "Verify pypgstac connection, collection name, and database permissions."
                )

            dataset.metadata_ = agg["metadata"]
            if agg["wkt"]:
                dataset.geometry = WKTElement(agg["wkt"], srid=4326)
            if agg["start_date"] and agg["end_date"]:
                dataset.temporal_extent = DateTimeTZRange(
                    agg["start_date"], agg["end_date"], bounds="[]"
                )

            dataset.status = DatasetStatus.READY
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()

        except PermanentTaskError as exc:
            session.rollback()
            job.status = JobStatus.FAILED
            job.logs = str(exc)
            job.finished_at = _now()
            dataset.status = DatasetStatus.FAILED
            session.commit()
            logger.error("Permanent ingestion failure: %s", exc)

        except Exception as exc:
            session.rollback()
            logger.exception("Transient error, will retry")
            raise self.retry(exc=exc)


@celery_app.task(ignore_result=True)
def refresh_annotation_statistics():
    """Refresh the annotation_statistics materialized view.

    Scheduled hourly by Celery beat and also called after bulk annotation ops.
    Routed to the 'default' queue via beat_schedule options.
    """
    with WorkerSession() as session:
        session.execute(
            text("REFRESH MATERIALIZED VIEW CONCURRENTLY annotation_statistics")
        )
        session.commit()
    logger.info("annotation_statistics materialized view refreshed")