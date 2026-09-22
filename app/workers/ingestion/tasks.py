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
from app.services.conversion import convert_geotiff_to_cog
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import INGESTION
from app.workers.ingestion.pypgstac_utils import upsert_stac_collection, upsert_stac_item, batch_upsert_stac_items
from app.workers.ingestion.rasterio_utils import (
    build_stac_collection,
    build_stac_item,
    extract_cog_metadata,
    is_cloud_optimized_geotiff,
    validate_cog,
)

logger = logging.getLogger(__name__)


def _publish_job_event(job):
    """Publish a job completion/failure event (fire-and-forget)."""
    try:
        from app.core.events import publish_sync
        event_type = "job.completed" if job.status == JobStatus.COMPLETED else "job.failed"
        publish_sync(str(job.organization_id), event_type, {
            "job_id": str(job.id),
            "job_type": job.type if isinstance(job.type, str) else job.type.value,
            "status": job.status if isinstance(job.status, str) else job.status.value,
            "progress": job.progress or 0,
        })
    except Exception:
        logger.debug("Failed to publish job event", exc_info=True)


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

def _safe_raster_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename((filename or "").replace("\\", "/")))[0]
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem).strip("._-")
    return cleaned[:80] or "raster"


def _processed_cog_object_key(dataset_id: uuid.UUID, filename: str) -> str:
    return storage_service.object_key(
        dataset_id, f"processed/{_safe_raster_stem(filename)}_cog.tif"
    )


def _processed_zip_cog_object_key(
    dataset_id: uuid.UUID, file_hash: str, filename: str
) -> str:
    digest = file_hash or "unknown"
    return storage_service.object_key(
        dataset_id, f"processed/{digest}_{_safe_raster_stem(filename)}_cog.tif"
    )


def _convert_if_not_cog(source_path: str, dest_path: str, gdal_env: dict) -> bool:
    is_valid, is_cog, issues = is_cloud_optimized_geotiff(source_path, gdal_env)
    if not is_valid:
        raise PermanentTaskError("\n".join(issues) or "Raster validation failed")
    if is_cog:
        return False
    logger.info(
        "non_cog_detected uri=%s issues=%s converting_to=%s",
        source_path, issues, dest_path,
    )
    try:
        convert_geotiff_to_cog(source_path, dest_path, gdal_env=gdal_env)
    except ValueError as exc:
        raise PermanentTaskError(
            f"GeoTIFF to COG conversion failed: {exc}"
        ) from exc
    return True


def _maybe_convert_non_cog(
    *,
    organization_id: uuid.UUID,
    dataset_id: uuid.UUID,
    s3_uri: str,
    filename: str,
    gdal_env: dict,
) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_out = os.path.join(tmpdir, f"{_safe_raster_stem(filename)}_cog.tif")
        converted = _convert_if_not_cog(s3_uri, tmp_out, gdal_env)
        if not converted:
            return s3_uri

        cog_key = _processed_cog_object_key(dataset_id, filename)
        bucket = storage_service.bucket_name(organization_id)
        converted_uri = f"s3://{bucket}/{cog_key}"
        storage_service.upload_from_path(
            organization_id, cog_key, tmp_out, content_type="image/tiff"
        )

    logger.info("cog_uploaded src=%s dest=%s", s3_uri, converted_uri)
    return converted_uri


def _prepare_zip_member_raster(
    *,
    organization_id: uuid.UUID,
    dataset_id: uuid.UUID,
    local_path: str,
    basename: str,
    file_hash: str,
    bucket: str,
    gdal_env: dict,
) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_out = os.path.join(tmpdir, f"{_safe_raster_stem(basename)}_cog.tif")
        converted = _convert_if_not_cog(local_path, tmp_out, gdal_env)
        if converted:
            cog_key = _processed_zip_cog_object_key(dataset_id, file_hash, basename)
            storage_service.upload_from_path(
                organization_id, cog_key, tmp_out, content_type="image/tiff"
            )
            uri = f"s3://{bucket}/{cog_key}"
            logger.info("cog_uploaded src=%s dest=%s", local_path, uri)
            return uri

    extracted_key = f"datasets/{dataset_id}/{file_hash}_{basename}"
    storage_service.upload_from_path(organization_id, extracted_key, local_path)
    return f"s3://{bucket}/{extracted_key}"


def _s3_key_from_uri(s3_uri: str) -> str:
    if not s3_uri or not s3_uri.startswith("s3://"):
        raise PermanentTaskError(f"Expected s3:// URI, got {s3_uri!r}")
    rest = s3_uri[5:]
    slash = rest.find("/")
    if slash < 0 or slash == len(rest) - 1:
        raise PermanentTaskError(f"s3 URI missing object key: {s3_uri!r}")
    return rest[slash + 1 :]


def _standalone_cog_object_key(
    output_dataset_id: uuid.UUID,
    source_item_id: uuid.UUID,
    filename: str,
) -> str:
    return storage_service.object_key(
        output_dataset_id,
        f"processed/{source_item_id}_{_safe_raster_stem(filename)}_cog.tif",
    )


def _materialize_standalone_cog(
    *,
    organization_id: uuid.UUID,
    output_dataset_id: uuid.UUID,
    source_item_id: uuid.UUID,
    s3_uri: str,
    filename: str,
    gdal_env: dict,
) -> tuple[str, bool]:
    """Copy or convert a source raster onto a new key under the output dataset.

    Returns ``(dest_s3_uri, was_already_cog)``. Temp files are always removed.
    """
    dest_key = _standalone_cog_object_key(output_dataset_id, source_item_id, filename)
    bucket = storage_service.bucket_name(organization_id)
    dest_uri = f"s3://{bucket}/{dest_key}"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_out = os.path.join(tmpdir, f"{_safe_raster_stem(filename)}_cog.tif")
        converted = _convert_if_not_cog(s3_uri, tmp_out, gdal_env)
        if converted:
            storage_service.upload_from_path(
                organization_id, dest_key, tmp_out, content_type="image/tiff"
            )
            logger.info("cog_uploaded src=%s dest=%s", s3_uri, dest_uri)
            return dest_uri, False

    storage_service.copy_object(
        organization_id, _s3_key_from_uri(s3_uri), dest_key
    )
    logger.info("cog_copied src=%s dest=%s", s3_uri, dest_uri)
    return dest_uri, True


def _deterministic_item_id(s3_uri: str) -> str:
    return hashlib.md5(s3_uri.encode()).hexdigest()

def _file_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _partition_zip_members(tif_members: list[str]) -> dict[str, list[str]]:
    """Group ZIP .tif members by their top-level folder.

    Returns a dict mapping folder names to member paths.  Files at the ZIP
    root (no folder) are collected under the ``"_root"`` key.

    Example::

        upload.zip/
          folder_A/img1.tif  →  {"folder_A": ["folder_A/img1.tif"]}
          folder_B/img2.tif  →  {"folder_B": ["folder_B/img2.tif"]}
          bare.tif            →  {"_root": ["bare.tif"]}
    """
    groups: dict[str, list[str]] = {}
    for member in tif_members:
        # Normalise forward slashes (ZIP standard) and strip leading ./
        clean = member.lstrip("./")
        parts = clean.split("/")
        if len(parts) == 1:
            groups.setdefault("_root", []).append(member)
        else:
            folder = parts[0]
            groups.setdefault(folder, []).append(member)
    return groups


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
             min(
                 coalesce(
                     datetime,
                     (content->'properties'->>'start_datetime')::timestamptz
                 )
             ) AS start_date,
             max(
                 coalesce(
                     (content->'properties'->>'end_datetime')::timestamptz,
                     datetime,
                     (content->'properties'->>'start_datetime')::timestamptz
                 )
             ) AS end_date,
             (SELECT content->'properties'->'rendering_config'
              FROM pgstac.items
              WHERE collection = :cid
              LIMIT 1) AS sample_rendering_config
        FROM pgstac.items
        WHERE collection = :cid
    """)

    with _get_pgstac_engine().connect() as conn:
        result = conn.execute(query, {"cid": collection}).mappings().first()

    if not result or result["file_count"] == 0:
        return None

    # rendering_config from the first item serves as collection-level default
    rendering_config = result["sample_rendering_config"]
    # psycopg2 may return a string if the JSONB is extracted via subquery
    if isinstance(rendering_config, str):
        import json as _json
        try:
            rendering_config = _json.loads(rendering_config)
        except (ValueError, TypeError):
            rendering_config = None

    return {
        "metadata": {
            "band_count": result["bands"] or [],
            "gsd_min": result["gsd_min"],
            "gsd_max": result["gsd_max"],
            "file_count": result["file_count"],
            "total_size_bytes": result["total_size"] or 0,
            "native_crs": result["native_crs"] or [],
            "rendering_config": rendering_config,
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

def _prepare_single_cog(
    s3_uri: str,
    filename: str,
    collection: str,
    s3_config: dict,
    dataset_type: str = "imagery",
) -> tuple[bool, list[str], str | None, dict | None]:
    """Validate a COG and prepare STAC item without inserting into pgSTAC.

    Returns (success, issues, stac_item_id, stac_item_dict).
    On failure stac_item_id and stac_item_dict are None.

    This is used for batch processing to avoid pgSTAC partition constraint issues.
    """
    is_valid, issues = validate_cog(s3_uri, s3_config)
    if not is_valid:
        return False, issues, None, None
    if issues:
        logger.warning("cog_warnings uri=%s warnings=%s", s3_uri, issues)

    metadata = extract_cog_metadata(s3_uri, s3_config, filename=filename, dataset_type=dataset_type)
    metadata["filename"] = filename
    item_id = _deterministic_item_id(s3_uri)

    item = build_stac_item(item_id, collection, s3_uri, metadata)
    
    return True, issues, item_id, item


def _ingest_single_cog(
    s3_uri: str,
    filename: str,
    collection: str,
    s3_config: dict,
    dataset_type: str = "imagery",
) -> tuple[bool, list[str], str | None, dict | None]:
    """Validate a COG and insert it as a STAC item.

    Returns (success, issues, stac_item_id, stac_item_dict).
    On failure stac_item_id and stac_item_dict are None.

    NOTE: This function inserts items one-at-a-time and can cause pgSTAC
    partition constraint issues. Use _prepare_single_cog + batch_upsert_stac_items
    for multi-item collections.
    """
    success, issues, item_id, item = _prepare_single_cog(
        s3_uri, filename, collection, s3_config, dataset_type,
    )
    
    if success and item:
        upsert_stac_item(item, settings.STAC_SYNC_DATABASE_URL)
    
    return success, issues, item_id, item


def _stac_item_datetime(properties: dict | None) -> datetime | None:
    """Resolve DatasetItem.item_datetime from STAC properties.

    For interval items, STAC sets ``datetime`` to null and uses
    ``start_datetime``/``end_datetime``. Use start_datetime as the canonical
    cached item timestamp.
    """
    props = properties or {}
    dt_str = props.get("datetime") or props.get("start_datetime") or props.get("end_datetime")
    if not dt_str:
        return None

    from dateutil import parser as dtparser  # noqa: PLC0415

    try:
        parsed = dtparser.isoparse(dt_str)
    except (TypeError, ValueError) as exc:
        logger.warning("invalid_stac_datetime value=%s error=%s", dt_str, exc)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
    properties_cache = stac_item.get("properties")
    item_datetime = _stac_item_datetime(properties_cache)

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
    
def _ingest_folder_group(
    session,
    job,
    dataset,
    bucket: str,
    collection: str,
    members: list[str],
    zf: zipfile.ZipFile,
    extract_base: str,
    gdal_env: dict,
    *,
    progress_offset: int = 0,
    total_across_all: int | None = None,
) -> tuple[int, list[str]]:
    """Ingest a group of .tif ZIP members into one dataset/collection.

    Uses batch STAC insert pattern to prevent pgSTAC partition constraint issues.

    Returns ``(processed_count, failed_files_list)``.

    *progress_offset* is the number of items already processed across
    earlier folder groups (for accurate ``job.progress`` tracking).
    *total_across_all* is the grand total of .tif files in the ZIP.
    """
    dataset_id = str(dataset.id)
    total_for_progress = total_across_all or len(members)
    processed = 0
    failed_files: list[str] = []
    
    # Collect STAC items for batch insert (prevents partition constraint issues)
    stac_items_to_insert: list[dict] = []

    for idx, member in enumerate(members):
        file_path = zf.extract(member, extract_base)
        basename = os.path.basename(file_path)

        try:
            fhash = _file_hash(file_path)
            try:
                s3_uri = _prepare_zip_member_raster(
                    organization_id=job.organization_id,
                    dataset_id=uuid.UUID(dataset_id),
                    local_path=file_path,
                    basename=basename,
                    file_hash=fhash,
                    bucket=bucket,
                    gdal_env=gdal_env,
                )
            except PermanentTaskError as exc:
                failed_files.append(f"{basename}: {exc}")
            else:
                # Use _prepare_single_cog to avoid individual pgSTAC inserts
                success, issues, item_id, stac_item = _prepare_single_cog(
                    s3_uri, basename, collection, gdal_env, dataset.dataset_type
                )

                if success:
                    processed += 1
                    job.processed_items = (job.processed_items or 0) + 1

                    # Add to batch for later pgSTAC insert
                    stac_items_to_insert.append(stac_item)

                    # Insert into app DB immediately for error recovery
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

        global_idx = progress_offset + idx + 1
        job.failed_items = (job.failed_items or 0)
        job.progress = global_idx / total_for_progress
        if global_idx % 10 == 0 or (idx + 1) == len(members):
            session.commit()

    # Batch insert all STAC items at once to prevent partition constraint issues
    if stac_items_to_insert:
        logger.info("Batch inserting %d STAC items for collection %s", 
                   len(stac_items_to_insert), collection)
        try:
            batch_upsert_stac_items(stac_items_to_insert, settings.STAC_SYNC_DATABASE_URL)
            logger.info("Successfully batch inserted %d STAC items", len(stac_items_to_insert))
        except Exception as exc:
            logger.error("Batch STAC insert failed for %d items: %s", 
                        len(stac_items_to_insert), exc, exc_info=True)
            # Add failed items to the failed_files list
            item_ids = [item.get('id', 'unknown') for item in stac_items_to_insert]
            failed_files.append(f"Batch pgSTAC insert failed for {len(stac_items_to_insert)} items: {item_ids[:3]}...")
            # Note: App DB items are already inserted, so data is not lost

    return processed, failed_files


def _ingest_zip(session, job, dataset, bucket, s3_key, dataset_id, gdal_env):
    """Download a ZIP, detect folder structure, and ingest accordingly.

    Single-folder (or flat) ZIPs: all items go into the pre-created dataset.
    Multi-folder ZIPs: each top-level folder becomes its own dataset/collection.
    """
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

            folder_groups = _partition_zip_members(tif_members)
            total = len(tif_members)
            job.total_items = total
            job.processed_items = 0
            job.failed_items = 0
            session.commit()

            extract_base = os.path.join(tmpdir, "extracted")
            is_multi = len(folder_groups) > 1

            if not is_multi:
                # --- Single folder / flat ZIP: backward-compatible path ---
                collection = _ensure_collection(session, dataset, job.organization_id)
                only_members = next(iter(folder_groups.values()))
                processed, failed = _ingest_folder_group(
                    session, job, dataset, bucket, collection,
                    only_members, zf, extract_base, gdal_env,
                    total_across_all=total,
                )
                if processed == 0:
                    raise PermanentTaskError(f"All files failed validation: {failed}")
                if failed:
                    job.logs = "Partial success – some files skipped:\n" + "\n".join(failed)
            else:
                # --- Multi-folder ZIP: one dataset per top-level folder ---
                logger.info(
                    "Multi-folder ZIP detected: %d folders, %d total files",
                    len(folder_groups), total,
                )
                created_datasets: list[Dataset] = []
                all_failed: list[str] = []
                progress_offset = 0

                for folder_idx, (folder_name, members) in enumerate(sorted(folder_groups.items())):
                    display_name = folder_name if folder_name != "_root" else os.path.splitext(os.path.basename(s3_key))[0]

                    if folder_idx == 0:
                        # Reuse the pre-created dataset for the first folder
                        ds = dataset
                        ds.name = display_name
                    else:
                        # Create a new Dataset for subsequent folders
                        ds = Dataset(
                            id=uuid.uuid4(),
                            organization_id=job.organization_id,
                            name=display_name,
                            dataset_type=dataset.dataset_type,
                            status=DatasetStatus.INGESTING,
                        )
                        session.add(ds)
                        session.flush()

                    created_datasets.append(ds)
                    collection = _ensure_collection(session, ds, job.organization_id)

                    processed, failed = _ingest_folder_group(
                        session, job, ds, bucket, collection,
                        members, zf, extract_base, gdal_env,
                        progress_offset=progress_offset,
                        total_across_all=total,
                    )
                    progress_offset += len(members)
                    all_failed.extend(failed)

                    # Finalize this dataset's metadata
                    if processed > 0:
                        agg = _compute_aggregated_metadata(ds.stac_collection_id)
                        if agg:
                            ds.metadata_ = agg["metadata"]
                            if agg["wkt"]:
                                ds.geometry = WKTElement(agg["wkt"], srid=4326)
                            if agg["start_date"] and agg["end_date"]:
                                ds.temporal_extent = DateTimeTZRange(
                                    agg["start_date"], agg["end_date"], bounds="[]"
                                )
                        ds.status = DatasetStatus.READY
                    else:
                        ds.status = DatasetStatus.FAILED
                    session.commit()

                # Store all created dataset IDs on the job
                created_ids = [str(ds.id) for ds in created_datasets]
                job.config = {**(job.config or {}), "created_dataset_ids": created_ids}
                job.input_refs = [{"type": "dataset", "id": did} for did in created_ids]

                any_succeeded = any(ds.status == DatasetStatus.READY for ds in created_datasets)
                if not any_succeeded:
                    raise PermanentTaskError(
                        f"All folders failed validation: {all_failed}"
                    )
                if all_failed:
                    job.logs = "Partial success – some files skipped:\n" + "\n".join(all_failed)
                session.commit()

            
def _fail_all_datasets(session, datasets: list, status=DatasetStatus.FAILED):
    """Mark all datasets in the list as FAILED."""
    for ds in datasets:
        ds.status = status


def _cleanup_zip_source(org_id: uuid.UUID, s3_key: str) -> None:
    """Delete the original ZIP from S3 after successful ingestion.

    The extracted .tif files (uploaded individually under datasets/{id}/)
    are the canonical data — the ZIP is no longer needed.
    """
    try:
        storage_service.delete_object(org_id, s3_key)
        logger.info("cleanup_zip_deleted org_id=%s key=%s", org_id, s3_key)
    except Exception:
        logger.warning("cleanup_zip_failed org_id=%s key=%s", org_id, s3_key, exc_info=True)


def _cleanup_failed_extracts(org_id: uuid.UUID, dataset_ids: list[str]) -> None:
    """Delete orphaned extracted files for failed datasets.

    On ingestion failure, any .tif files already uploaded under
    ``datasets/{dataset_id}/`` are orphans (no STAC items reference them
    successfully).  This removes them to avoid storage bloat.
    """
    for did in dataset_ids:
        try:
            n = storage_service.delete_objects_by_prefix(org_id, f"datasets/{did}/")
            if n > 0:
                logger.info("cleanup_orphans_deleted org_id=%s dataset_id=%s count=%d", org_id, did, n)
        except Exception:
            logger.warning("cleanup_orphans_failed org_id=%s dataset_id=%s", org_id, did, exc_info=True)


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

        created_datasets: list[Dataset] = [dataset]
        is_zip = filename.lower().endswith(".zip")
        is_multi_folder_zip = False

        try:
            if job.organization_id != dataset.organization_id:
                raise PermanentTaskError(
                    "Job and dataset organization mismatch — refusing ingestion"
                )

            bucket = storage_service.bucket_name(job.organization_id)
            gdal_env = _gdal_env_for_worker()

            if is_zip:
                _ingest_zip(session, job, dataset, bucket, s3_key, dataset_id, gdal_env)
                # Check if multi-folder mode was used
                created_ids = (job.config or {}).get("created_dataset_ids")
                if created_ids and len(created_ids) > 1:
                    is_multi_folder_zip = True
                    # Reload all created datasets for error handling
                    created_datasets = [
                        session.get(Dataset, uuid.UUID(did)) for did in created_ids
                    ]
                    created_datasets = [ds for ds in created_datasets if ds is not None]
            else:
                collection = _ensure_collection(session, dataset, job.organization_id)
                source_s3_uri = f"s3://{bucket}/{s3_key}"
                s3_uri = _maybe_convert_non_cog(
                    organization_id=job.organization_id,
                    dataset_id=dataset.id,
                    s3_uri=source_s3_uri,
                    filename=filename,
                    gdal_env=gdal_env,
                )
                success, issues, item_id, stac_item = _ingest_single_cog(
                    s3_uri, filename, collection, gdal_env, dataset.dataset_type
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

            if not is_multi_folder_zip:
                # Single file or single-folder ZIP: aggregate metadata for
                # the one dataset (multi-folder ZIPs handle this per-folder).
                agg = _compute_aggregated_metadata(dataset.stac_collection_id)
                if agg is None:
                    raise PermanentTaskError(
                        f"Ingestion completed but no items found in pgSTAC for "
                        f"collection {dataset.stac_collection_id!r}. "
                        "Verify pypgstac connection, collection name, and permissions."
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
            _publish_job_event(job)

            # --- Dispatch automation event for dataset.ingested trigger ---
            try:
                from app.automation.event_dispatcher import dispatch_event_sync
                for dataset in created_datasets:
                    dispatch_event_sync(
                        session,
                        str(job.organization_id),
                        "dataset.ingested",
                        {"dataset_id": str(dataset.id)},
                    )
            except Exception:
                logger.warning("dispatch_event_sync failed for datasets", exc_info=True)

            # --- Cleanup: delete the original ZIP after successful ingestion ---
            if is_zip:
                _cleanup_zip_source(job.organization_id, s3_key)

        except PermanentTaskError as exc:
            session.rollback()
            job.status = JobStatus.FAILED
            job.logs = str(exc)
            job.finished_at = _now()
            _fail_all_datasets(session, created_datasets)
            session.commit()
            _publish_job_event(job)
            logger.error("Permanent ingestion failure: %s", exc)

            # --- Cleanup: remove orphaned extracted files for failed datasets ---
            failed_ids = [str(ds.id) for ds in created_datasets if ds.status == DatasetStatus.FAILED]
            if is_zip and failed_ids:
                _cleanup_failed_extracts(job.organization_id, failed_ids)

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


# ── Stale Job Cleanup ────────────────────────────────────────────────────────

# Default threshold: jobs pending/queued for more than 24 hours are stale
STALE_JOB_THRESHOLD_HOURS = int(os.environ.get("STALE_JOB_THRESHOLD_HOURS", "24"))

RUNNING_JOB_TIMEOUT_MINUTES = int(os.environ.get("RUNNING_JOB_TIMEOUT_MINUTES", "10"))


@celery_app.task(ignore_result=True)
def cleanup_stale_pending_jobs():
    """Mark stale pending/queued jobs as failed and clean up associated files.

    A job is considered stale if:
    - Status is 'pending' or 'queued'
    - Created more than STALE_JOB_THRESHOLD_HOURS ago (default: 24h)

    For each stale job:
    1. Mark job as 'failed' with explanation
    2. Mark associated dataset as 'failed' (if any)
    3. Abort any in-progress multipart uploads
    4. Delete any orphaned files in MinIO

    Scheduled hourly by Celery beat. Safe to run multiple times.
    """
    from sqlalchemy import select as sync_select

    threshold_hours = STALE_JOB_THRESHOLD_HOURS

    with WorkerSession() as session:
        # Find stale jobs
        stale_jobs = session.execute(
            text("""
                SELECT id, organization_id, config, input_refs, type
                FROM jobs
                WHERE status IN ('pending', 'queued')
                  AND created_at < NOW() - INTERVAL ':hours hours'
            """),
            {"hours": threshold_hours},
        ).mappings().all()

        if not stale_jobs:
            logger.debug("cleanup_stale_jobs no stale jobs found")
            return

        logger.info(
            "cleanup_stale_jobs found %d jobs older than %d hours",
            len(stale_jobs), threshold_hours,
        )

        for job_row in stale_jobs:
            job_id = job_row["id"]
            org_id = job_row["organization_id"]
            config = job_row["config"] or {}
            input_refs = job_row["input_refs"] or []
            job_type = job_row["type"]

            try:
                # 1. Mark job as failed
                session.execute(
                    text("""
                        UPDATE jobs
                        SET status = 'failed',
                            logs = COALESCE(logs || E'\\n', '') ||
                                   'Job timed out: no worker processed this job within :hours hours. ' ||
                                   'This may indicate an abandoned upload or queue backlog.',
                            finished_at = NOW()
                        WHERE id = :job_id
                    """),
                    {"job_id": job_id, "hours": threshold_hours},
                )

                # 2. Mark associated datasets as failed
                dataset_ids: list[str] = []

                # Check input_refs for dataset references
                for ref in input_refs:
                    if isinstance(ref, dict) and ref.get("type") == "dataset":
                        did = ref.get("id")
                        if did:
                            dataset_ids.append(did)

                # Check config for dataset_id (ingest jobs)
                if job_type == "ingest" and config.get("dataset_id"):
                    dataset_ids.append(config["dataset_id"])

                # Also check created_dataset_ids (multi-folder zip)
                for did in config.get("created_dataset_ids", []):
                    if did not in dataset_ids:
                        dataset_ids.append(did)

                for did in dataset_ids:
                    try:
                        session.execute(
                            text("""
                                UPDATE datasets
                                SET status = 'failed'
                                WHERE id = :did
                                  AND status IN ('pending', 'ingesting')
                            """),
                            {"did": did},
                        )
                    except Exception:
                        logger.warning(
                            "cleanup_stale_jobs failed to update dataset %s", did,
                            exc_info=True,
                        )

                session.commit()

                # 3. Abort stale multipart uploads for this org
                try:
                    aborted = storage_service.abort_stale_multipart_uploads(
                        org_id, older_than_hours=threshold_hours
                    )
                    if aborted:
                        logger.info(
                            "cleanup_stale_jobs aborted %d stale uploads for org %s",
                            aborted, org_id,
                        )
                except Exception:
                    logger.warning(
                        "cleanup_stale_jobs failed to abort uploads for org %s",
                        org_id, exc_info=True,
                    )

                # 4. Clean up orphaned files for failed datasets
                if dataset_ids:
                    _cleanup_failed_extracts(org_id, dataset_ids)

                logger.info(
                    "cleanup_stale_jobs marked job %s as failed (datasets: %s)",
                    job_id, dataset_ids,
                )

            except Exception:
                session.rollback()
                logger.exception(
                    "cleanup_stale_jobs failed to process job %s", job_id
                )

    logger.info("cleanup_stale_jobs completed")


@celery_app.task(ignore_result=True)
def cleanup_stale_running_jobs():
    """Mark running/ingesting jobs as failed if they exceed RUNNING_JOB_TIMEOUT_MINUTES.

    A job is considered stale if:
    - Status is 'running' AND started_at > RUNNING_JOB_TIMEOUT_MINUTES ago
    - OR the associated dataset has status 'ingesting' for > RUNNING_JOB_TIMEOUT_MINUTES

    For each stale job:
    1. Mark job as 'failed' with timeout explanation
    2. Mark associated dataset as 'failed'
    3. Delete any orphaned extracted files from S3

    Scheduled every 2 minutes by Celery beat. Safe to run multiple times.
    """
    timeout_minutes = RUNNING_JOB_TIMEOUT_MINUTES

    with WorkerSession() as session:
        # Find running jobs that have exceeded the timeout
        stale_jobs = session.execute(
            text("""
                SELECT j.id, j.organization_id, j.config, j.input_refs, j.type
                FROM jobs j
                WHERE j.status = 'running'
                  AND j.started_at < NOW() - INTERVAL ':minutes minutes'
                  AND j.type = 'ingest'
            """),
            {"minutes": timeout_minutes},
        ).mappings().all()

        # Also find datasets stuck in 'ingesting' state with no running job
        stale_datasets = session.execute(
            text("""
                SELECT d.id, d.organization_id
                FROM datasets d
                WHERE d.status = 'ingesting'
                  AND d.updated_at < NOW() - INTERVAL ':minutes minutes'
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.status = 'running'
                        AND j.type = 'ingest'
                        AND (
                            j.config->>'dataset_id' = d.id::text
                            OR j.config->'created_dataset_ids' ? d.id::text
                        )
                  )
            """),
            {"minutes": timeout_minutes},
        ).mappings().all()

        if not stale_jobs and not stale_datasets:
            logger.debug("cleanup_stale_running_jobs: no stale jobs/datasets found")
            return

        if stale_jobs:
            logger.info(
                "cleanup_stale_running_jobs found %d jobs running longer than %d minutes",
                len(stale_jobs), timeout_minutes,
            )

        if stale_datasets:
            logger.info(
                "cleanup_stale_running_jobs found %d orphaned ingesting datasets",
                len(stale_datasets),
            )

        # Process stale jobs
        for job_row in stale_jobs:
            job_id = job_row["id"]
            org_id = job_row["organization_id"]
            config = job_row["config"] or {}

            try:
                # 1. Mark job as failed
                session.execute(
                    text("""
                        UPDATE jobs
                        SET status = 'failed',
                            logs = COALESCE(logs || E'\\n', '') ||
                                   'Job timed out: ingestion exceeded :minutes minute limit. ' ||
                                   'This may indicate a processing issue or oversized file.',
                            finished_at = NOW()
                        WHERE id = :job_id
                    """),
                    {"job_id": job_id, "minutes": timeout_minutes},
                )

                # 2. Collect and fail associated datasets
                dataset_ids: list[str] = []
                if config.get("dataset_id"):
                    dataset_ids.append(config["dataset_id"])
                for did in config.get("created_dataset_ids", []):
                    if did not in dataset_ids:
                        dataset_ids.append(did)

                for did in dataset_ids:
                    session.execute(
                        text("""
                            UPDATE datasets
                            SET status = 'failed'
                            WHERE id = :did
                              AND status IN ('pending', 'ingesting')
                        """),
                        {"did": did},
                    )

                session.commit()

                # 3. Clean up orphaned extracted files
                if dataset_ids:
                    _cleanup_failed_extracts(org_id, dataset_ids)

                logger.warning(
                    "cleanup_stale_running_jobs: marked job %s as failed after %d min timeout (datasets: %s)",
                    job_id, timeout_minutes, dataset_ids,
                )

            except Exception:
                session.rollback()
                logger.exception(
                    "cleanup_stale_running_jobs failed to process job %s", job_id
                )

        # Process orphaned ingesting datasets (no associated running job)
        for ds_row in stale_datasets:
            did = ds_row["id"]
            org_id = ds_row["organization_id"]

            try:
                session.execute(
                    text("""
                        UPDATE datasets
                        SET status = 'failed'
                        WHERE id = :did
                    """),
                    {"did": did},
                )
                session.commit()

                # Clean up any extracted files
                _cleanup_failed_extracts(org_id, [str(did)])

                logger.warning(
                    "cleanup_stale_running_jobs: marked orphaned dataset %s as failed",
                    did,
                )

            except Exception:
                session.rollback()
                logger.exception(
                    "cleanup_stale_running_jobs failed to process dataset %s", did
                )

    logger.info("cleanup_stale_running_jobs completed")


@celery_app.task(bind=True, queue=INGESTION, max_retries=2, default_retry_delay=60)
def rasterize_annotation_set(self, job_id: str) -> None:
    """Rasterize vector annotation set(s) into a segmentation-mask COG dataset.

    Runs on the ``ingestion`` queue (not bulk): it needs the GDAL runtime *and*
    pgSTAC network access to ingest the generated COG as a dataset — exactly the
    ingestion worker's environment. Reads ``job.config``:

    ``annotation_set_ids``   vector sets to burn (required)
    ``reference_dataset_id`` dataset whose native CRS + resolution define the
                             output grid, so the mask aligns with the raster it
                             will be compared to (optional)
    ``resolution_m``         explicit ground sampling distance override (optional)
    ``dataset_name``         output dataset name (optional)
    ``automation_run_id``/``automation_step_id``  present when dispatched from a
                             pipeline node — triggers ``resume_after_job`` so the
                             waiting step continues with ``{"dataset": {...}}``.

    On success the new ``segmentation_mask`` dataset is created, ingested, and
    its ``rendering_config.class_map`` set so it renders with class colors.
    """
    from sqlalchemy import select

    from app.core.enums import DatasetType
    from app.models.annotation_set import AnnotationSet
    from app.services.conversion import rasterize_annotation_sets_to_cog

    job_uuid = uuid.UUID(job_id)
    output_data: dict | None = None
    automation_run_id = automation_step_id = None
    tmp_path: str | None = None

    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("rasterize_annotation_set: job %s not found", job_id)
            return
        cfg = dict(job.config or {})
        automation_run_id = cfg.get("automation_run_id")
        automation_step_id = cfg.get("automation_step_id")
        try:
            job.status = JobStatus.RUNNING
            job.started_at = _now()
            session.commit()

            set_ids = [uuid.UUID(str(s)) for s in (cfg.get("annotation_set_ids") or []) if s]
            if not set_ids:
                raise ValueError("job.config.annotation_set_ids is required")
            sets = session.execute(
                select(AnnotationSet).where(AnnotationSet.id.in_(set_ids))
            ).scalars().all()
            if len(sets) != len(set_ids):
                raise ValueError("one or more annotation sets not found")
            if any(s.organization_id != job.organization_id for s in sets):
                raise ValueError("annotation set does not belong to this organization")

            reference_item_uri = None
            ref_ds_id = cfg.get("reference_dataset_id")
            if ref_ds_id:
                ref_item = session.execute(
                    select(DatasetItem)
                    .where(
                        DatasetItem.dataset_id == uuid.UUID(str(ref_ds_id)),
                        DatasetItem.organization_id == job.organization_id,
                        DatasetItem.is_active.is_(True),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if ref_item is not None:
                    reference_item_uri = ref_item.s3_uri

            resolution_m = cfg.get("resolution_m")
            fd, tmp_path = tempfile.mkstemp(suffix=".tif")
            os.close(fd)
            result = rasterize_annotation_sets_to_cog(
                session,
                set_ids,
                tmp_path,
                gdal_env=_gdal_env_for_worker(),
                reference_item_uri=reference_item_uri,
                resolution_m=float(resolution_m) if resolution_m not in (None, "") else None,
            )

            dataset = Dataset(
                id=uuid.uuid4(),
                organization_id=job.organization_id,
                name=cfg.get("dataset_name") or f"{sets[0].name} · mask",
                dataset_type=DatasetType.SEGMENTATION_MASK.value,
                status=DatasetStatus.INGESTING,
                created_by=job.created_by_user_id,
            )
            session.add(dataset)
            session.flush()

            filename = f"{dataset.id}.tif"
            s3_key = storage_service.object_key(dataset.id, filename)
            storage_service.ensure_org_bucket(job.organization_id)
            storage_service.upload_from_path(
                job.organization_id, s3_key, tmp_path, content_type="image/tiff"
            )
            bucket = storage_service.bucket_name(job.organization_id)
            s3_uri = f"s3://{bucket}/{s3_key}"

            collection = _ensure_collection(session, dataset, job.organization_id)
            success, issues, item_id, stac_item = _ingest_single_cog(
                s3_uri, filename, collection,
                _gdal_env_for_worker(), DatasetType.SEGMENTATION_MASK.value,
            )
            if not success:
                raise ValueError("\n".join(issues) or "COG validation failed")
            _upsert_dataset_item(
                session,
                dataset_id=dataset.id,
                organization_id=job.organization_id,
                stac_item_id=item_id,
                stac_collection_id=collection,
                s3_uri=s3_uri,
                filename=filename,
                stac_item=stac_item,
            )

            # Build the class_map the segmentation-mask render path expects, and
            # persist it on both the dataset and its item rendering_config.
            class_map = {
                "schema_id": str(result.schema_id) if result.schema_id else None,
                "band_index": result.band_index,
                "nodata_value": result.nodata_value,
                "value_class_map": result.value_class_map,
            }
            agg = _compute_aggregated_metadata(dataset.stac_collection_id)
            meta = dict((agg or {}).get("metadata") or {})
            rc = dict(meta.get("rendering_config") or {})
            rc["class_map"] = class_map
            meta["rendering_config"] = rc
            dataset.metadata_ = meta
            if agg and agg.get("wkt"):
                dataset.geometry = WKTElement(agg["wkt"], srid=4326)
            dataset.status = DatasetStatus.READY

            ds_item = session.execute(
                select(DatasetItem).where(DatasetItem.stac_item_id == item_id)
            ).scalar_one_or_none()
            if ds_item is not None:
                props = dict(ds_item.properties_cache or {})
                item_rc = dict(props.get("rendering_config") or {})
                item_rc["class_map"] = class_map
                props["rendering_config"] = item_rc
                ds_item.properties_cache = props

            cfg["result"] = {
                "dataset_id": str(dataset.id),
                "value_class_map": result.value_class_map,
                "feature_count": result.feature_count,
                "width": result.width,
                "height": result.height,
                "crs": result.crs,
            }
            job.config = cfg
            job.processed_items = 1
            job.total_items = 1
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()
            logger.info(
                "rasterize_annotation_set done job=%s dataset=%s size=%dx%d classes=%d",
                job_id, dataset.id, result.width, result.height, len(result.value_class_map),
            )

            output_data = {
                "dataset": {
                    "id": str(dataset.id),
                    "name": dataset.name,
                    "dataset_type": DatasetType.SEGMENTATION_MASK.value,
                    "value_class_map": result.value_class_map,
                    "class_map": class_map,
                }
            }
        except Exception as exc:
            session.rollback()
            logger.exception("rasterize_annotation_set failed job=%s", job_id)
            try:
                job = session.get(Job, job_uuid)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.logs = str(exc)[:4000]
                    job.finished_at = _now()
                    session.commit()
            except Exception:
                logger.exception("could not mark job %s as failed", job_id)
            raise
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.warning("could not remove temp COG %s", tmp_path)

    # Resume the automation pipeline after the session closes so the follow-up
    # step reads committed Job/Dataset state.
    if automation_run_id and automation_step_id and output_data is not None:
        from app.workers.automation.tasks import resume_after_job  # noqa: PLC0415

        resume_after_job.delay(job_id, output_data)


def _active_source_items(
    session,
    *,
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    item_ids: list[uuid.UUID] | None,
) -> list[DatasetItem]:
    from sqlalchemy import select as sync_select  # noqa: PLC0415

    q = sync_select(DatasetItem).where(
        DatasetItem.dataset_id == dataset_id,
        DatasetItem.organization_id == organization_id,
        DatasetItem.is_active.is_(True),
    )
    if item_ids:
        q = q.where(DatasetItem.id.in_(item_ids))
    return list(session.execute(q).scalars().all())


def _output_items_by_source_id(session, output_dataset_id: uuid.UUID) -> dict[str, DatasetItem]:
    from sqlalchemy import select as sync_select  # noqa: PLC0415

    rows = session.execute(
        sync_select(DatasetItem).where(
            DatasetItem.dataset_id == output_dataset_id,
            DatasetItem.is_active.is_(True),
        )
    ).scalars().all()
    mapping: dict[str, DatasetItem] = {}
    for row in rows:
        sid = (row.properties_cache or {}).get("source_item_id")
        if sid:
            mapping[str(sid)] = row
    return mapping


def _get_or_create_convert_output_dataset(session, job, cfg: dict, source: Dataset) -> Dataset:
    existing_id = cfg.get("output_dataset_id")
    if existing_id:
        ds = session.get(Dataset, uuid.UUID(str(existing_id)))
        if ds is None:
            raise PermanentTaskError(
                f"output dataset {existing_id} recorded on job but not found"
            )
        if ds.organization_id != job.organization_id:
            raise PermanentTaskError("output dataset organization mismatch")
        if ds.status != DatasetStatus.INGESTING:
            ds.status = DatasetStatus.INGESTING
        return ds

    output = Dataset(
        id=uuid.uuid4(),
        organization_id=job.organization_id,
        name=(cfg.get("dataset_name") or "").strip() or f"{source.name} · COG",
        description=source.description,
        dataset_type=source.dataset_type,
        status=DatasetStatus.INGESTING,
        created_by=job.created_by_user_id,
    )
    session.add(output)
    session.flush()
    cfg["output_dataset_id"] = str(output.id)
    job.config = dict(cfg)
    session.commit()
    return output


def _stac_item_with_source_rendering(source_item: DatasetItem, stac_item: dict) -> dict:
    item = dict(stac_item)
    props = dict(item.get("properties") or {})
    src_props = source_item.properties_cache or {}
    if isinstance(src_props, dict) and src_props.get("rendering_config"):
        props["rendering_config"] = dict(src_props["rendering_config"])
    props["source_item_id"] = str(source_item.id)
    item["properties"] = props
    return item


def _merge_source_rendering_metadata(source: Dataset, agg_meta: dict) -> dict:
    meta = dict(agg_meta or {})
    src_meta = dict(source.metadata_ or {})
    src_rc = dict(src_meta.get("rendering_config") or {})
    agg_rc = dict(meta.get("rendering_config") or {})
    if src_rc:
        meta["rendering_config"] = {**agg_rc, **src_rc}
    elif agg_rc:
        meta["rendering_config"] = agg_rc
    return meta


@celery_app.task(bind=True, queue=INGESTION, max_retries=2, default_retry_delay=60)
def convert_dataset_to_cog(self, job_id: str) -> None:
    """Convert a source dataset's rasters into a sibling COG dataset.

    Reads ``job.config``:

    ``source_dataset_id``   required
    ``dataset_item_ids``    optional subset; otherwise all active items
    ``dataset_name``        optional output name (default ``'{source} · COG'``)
    ``output_dataset_id``   persisted on first run and reused on Celery retry
    ``project_id``          optional ProjectDataset link on success
    ``automation_run_id``/``automation_step_id``  resume a waiting pipeline step

    The source Dataset is never modified. Output stays ``ingesting`` until every
    selected item is copied/converted, registered in MinIO + pgSTAC + DatasetItem,
    and aggregated metadata is written.
    """
    from sqlalchemy import select as sync_select  # noqa: PLC0415

    from app.models.job_output import JobOutput
    from app.models.project_dataset import ProjectDataset

    job_uuid = uuid.UUID(job_id)
    output_data: dict | None = None
    automation_run_id = automation_step_id = None

    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("convert_dataset_to_cog: job %s not found", job_id)
            return
        cfg = dict(job.config or {})
        automation_run_id = cfg.get("automation_run_id")
        automation_step_id = cfg.get("automation_step_id")
        try:
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or _now()
            session.commit()

            source_id = cfg.get("source_dataset_id")
            if not source_id:
                raise PermanentTaskError("job.config.source_dataset_id is required")
            source = session.get(Dataset, uuid.UUID(str(source_id)))
            if source is None or source.deleted_at is not None:
                raise PermanentTaskError("source dataset not found")
            if source.organization_id != job.organization_id:
                raise PermanentTaskError(
                    "Job and dataset organization mismatch — refusing conversion"
                )

            configured_ids = [
                uuid.UUID(str(i)) for i in (cfg.get("dataset_item_ids") or []) if i
            ]
            items = _active_source_items(
                session,
                dataset_id=source.id,
                organization_id=job.organization_id,
                item_ids=configured_ids or None,
            )
            if configured_ids and len(items) != len(set(configured_ids)):
                raise PermanentTaskError("one or more dataset items were not found")
            if not items:
                raise PermanentTaskError("source dataset has no active raster items")

            job.total_items = len(items)
            job.processed_items = 0
            job.failed_items = 0
            job.progress = 0.0
            session.commit()

            output = _get_or_create_convert_output_dataset(session, job, cfg, source)
            cfg = dict(job.config or cfg)
            already_done = _output_items_by_source_id(session, output.id)

            gdal_env = _gdal_env_for_worker()
            storage_service.ensure_org_bucket(job.organization_id)
            collection = _ensure_collection(session, output, job.organization_id)

            failed_files: list[str] = []
            prepared: list[tuple[DatasetItem, str, bool, str, dict]] = []
            skipped_already_cog = 0
            converted_count = 0

            for idx, source_item in enumerate(items):
                existing = already_done.get(str(source_item.id))
                if existing is not None:
                    was_cog = bool((existing.properties_cache or {}).get("was_already_cog"))
                    prepared.append(
                        (
                            source_item,
                            existing.s3_uri,
                            was_cog,
                            existing.stac_item_id,
                            {
                                "geometry": existing.geometry,
                                "properties": existing.properties_cache or {},
                            },
                        )
                    )
                    if was_cog:
                        skipped_already_cog += 1
                    else:
                        converted_count += 1
                    job.processed_items = idx + 1
                    job.progress = job.processed_items / job.total_items
                    session.commit()
                    continue
                try:
                    dest_uri, was_already_cog = _materialize_standalone_cog(
                        organization_id=job.organization_id,
                        output_dataset_id=output.id,
                        source_item_id=source_item.id,
                        s3_uri=source_item.s3_uri,
                        filename=source_item.filename,
                        gdal_env=gdal_env,
                    )
                    success, issues, item_id, stac_item = _prepare_single_cog(
                        dest_uri,
                        source_item.filename,
                        collection,
                        gdal_env,
                        output.dataset_type,
                    )
                    if not success or stac_item is None or item_id is None:
                        failed_files.append(
                            f"{source_item.filename}: {'; '.join(issues) or 'COG validation failed'}"
                        )
                    else:
                        stac_item = _stac_item_with_source_rendering(source_item, stac_item)
                        stac_item["properties"]["was_already_cog"] = was_already_cog
                        prepared.append(
                            (source_item, dest_uri, was_already_cog, item_id, stac_item)
                        )
                        if was_already_cog:
                            skipped_already_cog += 1
                        else:
                            converted_count += 1
                except PermanentTaskError as exc:
                    failed_files.append(f"{source_item.filename}: {exc}")
                job.processed_items = idx + 1
                job.failed_items = len(failed_files)
                job.progress = job.processed_items / job.total_items
                session.commit()

            if failed_files:
                job.logs = "\n".join(failed_files)[:4000]
                raise PermanentTaskError(
                    f"{len(failed_files)} of {len(items)} item(s) failed conversion"
                )

            new_stac = []
            for source_item, _uri, _cog, _iid, stac_item in prepared:
                if str(source_item.id) not in already_done:
                    new_stac.append(stac_item)
            if new_stac:
                batch_upsert_stac_items(new_stac, settings.STAC_SYNC_DATABASE_URL)

            result_items = []
            for source_item, dest_uri, was_already_cog, item_id, stac_item in prepared:
                if str(source_item.id) not in already_done:
                    _upsert_dataset_item(
                        session,
                        dataset_id=output.id,
                        organization_id=job.organization_id,
                        stac_item_id=item_id,
                        stac_collection_id=collection,
                        s3_uri=dest_uri,
                        filename=source_item.filename,
                        stac_item=stac_item,
                    )
                result_items.append(
                    {
                        "source_item_id": str(source_item.id),
                        "filename": source_item.filename,
                        "s3_uri": dest_uri,
                        "was_already_cog": was_already_cog,
                    }
                )

            agg = _compute_aggregated_metadata(output.stac_collection_id)
            if agg is None:
                raise PermanentTaskError(
                    f"Conversion completed but no items found in pgSTAC for "
                    f"collection {output.stac_collection_id!r}."
                )
            output.metadata_ = _merge_source_rendering_metadata(
                source, dict(agg.get("metadata") or {})
            )
            if agg.get("wkt"):
                output.geometry = WKTElement(agg["wkt"], srid=4326)
            if agg.get("start_date") and agg.get("end_date"):
                output.temporal_extent = DateTimeTZRange(
                    agg["start_date"], agg["end_date"], bounds="[]"
                )
            output.status = DatasetStatus.READY

            project_id = cfg.get("project_id")
            if project_id:
                proj_uuid = uuid.UUID(str(project_id))
                existing_link = session.get(ProjectDataset, (proj_uuid, output.id))
                if existing_link is None:
                    session.add(
                        ProjectDataset(
                            project_id=proj_uuid,
                            dataset_id=output.id,
                            linked_by=job.created_by_user_id,
                        )
                    )

            existing_output = session.execute(
                sync_select(JobOutput).where(
                    JobOutput.job_id == job.id,
                    JobOutput.output_id == output.id,
                )
            ).scalar_one_or_none()
            if existing_output is None:
                session.add(
                    JobOutput(
                        job_id=job.id,
                        output_type="dataset",
                        output_id=output.id,
                    )
                )

            cfg["result"] = {
                "source_dataset_id": str(source.id),
                "dataset_id": str(output.id),
                "converted_count": converted_count,
                "skipped_already_cog": skipped_already_cog,
                "failed_count": 0,
                "items": result_items,
            }
            job.config = dict(cfg)
            job.processed_items = len(items)
            job.failed_items = 0
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()
            _publish_job_event(job)
            logger.info(
                "convert_dataset_to_cog done job=%s source=%s output=%s items=%d",
                job_id, source.id, output.id, len(items),
            )
            output_data = {
                "dataset": {
                    "id": str(output.id),
                    "name": output.name,
                    "dataset_type": output.dataset_type,
                }
            }
        except PermanentTaskError as exc:
            session.rollback()
            job = session.get(Job, job_uuid)
            if job is not None:
                fail_cfg = dict(job.config or {})
                job.status = JobStatus.FAILED
                job.logs = str(exc)[:4000]
                job.finished_at = _now()
                out_id = fail_cfg.get("output_dataset_id")
                if out_id:
                    failed_ds = session.get(Dataset, uuid.UUID(str(out_id)))
                    if failed_ds is not None and failed_ds.status != DatasetStatus.READY:
                        failed_ds.status = DatasetStatus.FAILED
                session.commit()
                _publish_job_event(job)
            logger.error("convert_dataset_to_cog permanent failure job=%s: %s", job_id, exc)
        except Exception as exc:
            session.rollback()
            logger.exception("convert_dataset_to_cog transient error job=%s", job_id)
            raise self.retry(exc=exc)

    if automation_run_id and automation_step_id and output_data is not None:
        from app.workers.automation.tasks import resume_after_job  # noqa: PLC0415

        resume_after_job.delay(job_id, output_data)
