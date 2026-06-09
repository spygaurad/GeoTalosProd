import asyncio
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.enums import DatasetStatus, DatasetType, JobStatus, JobType
from app.core.exceptions import not_found
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User
from app.schemas.dataset import (
    DatasetClassMapRead,
    DatasetClassMapUpdate,
    DatasetCreate,
    DatasetListResponse,
    DatasetRasterValuesRead,
    DatasetRead,
    DatasetUpdate,
    PartUrlsRequest,
    PartUrlsResponse,
    UploadCompleteRequest,
    UploadInitiateRequest,
    UploadInitiateResponse,
    UploadJobResponse,
    UploadPartUrl,
)
from app.schemas.dataset_item import DatasetItemListResponse, DatasetItemTileConfig
from app.services.dataset_service import DatasetService
from app.services import storage_service
from app.services import titiler_service
from app.workers.ingestion.rasterio_utils import extract_unique_values

router = APIRouter(prefix="/datasets", tags=["datasets"])

# Limit concurrent rasterio S3 reads to prevent thread pool exhaustion.
_raster_preview_semaphore = asyncio.Semaphore(2)


def _gdal_env_for_api() -> dict:
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
        "GDAL_HTTP_TIMEOUT": "30",
        "CPL_CURL_GZIP": "YES",
    }


def _normalize_value_key(value: object) -> str:
    """Normalize a pixel value to a class-map key (integral → "5", else "5.5")."""
    fv = float(str(value).strip())
    return str(int(fv)) if fv.is_integer() else str(fv)

# Part size used for multipart uploads: 50 MiB.
# Larger parts = fewer round trips = faster uploads. MinIO / S3 minimum is
# 5 MiB per part (except the last); max 10 000 parts per upload.
# 50 MiB × 10 000 = ~488 GiB max file size.
_PART_SIZE_BYTES = 50 * 1024 * 1024
# Number of presigned part URLs returned in the initiate response.
# For most files (< 500 MiB) this covers all parts in a single response.
_INITIAL_PART_BATCH = 50


async def _get_upload_job(
    db: AsyncSession,
    upload_id: str,
    org_id: UUID,
    dataset_id: UUID,
) -> Job:
    """Load the ingest Job for the given upload_id, scoped to org and dataset."""
    result = await db.execute(
        select(Job).where(
            Job.organization_id == org_id,
            Job.config["upload_id"].astext == upload_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise not_found("Upload job")
    refs = job.input_refs or []
    if not any(r.get("id") == str(dataset_id) for r in refs):
        raise not_found("Upload job")
    return job


@router.get("", response_model=DatasetListResponse)
async def list_datasets(
    organization_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    if organization_id is not None and organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = DatasetService(db)
    items, total = await service.list_datasets(
        limit=limit,
        offset=offset,
        organization_id=org_id,
    )
    return DatasetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{dataset_id}", response_model=DatasetRead)
async def get_dataset_by_id(
    dataset_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    return await service.get_dataset(dataset_id, organization_id=org_id)


@router.post("", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    payload: DatasetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    dataset = await service.create_dataset(payload, organization_id=org_id)
    await log_audit_event(
        action="datasets.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset.id),
        session=db,
    )
    return dataset


@router.patch("/{dataset_id}", response_model=DatasetRead)
async def update_dataset_by_id(
    dataset_id: UUID,
    payload: DatasetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    dataset = await service.update_dataset(dataset_id, payload, organization_id=org_id)
    await log_audit_event(
        action="datasets.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset_id),
        session=db,
    )
    return dataset


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset_by_id(
    dataset_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    await service.delete_dataset(dataset_id, organization_id=org_id)
    await log_audit_event(
        action="datasets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset_id),
        session=db,
    )


# ── Dataset items ─────────────────────────────────────────────────────────────


@router.get("/{dataset_id}/items", response_model=DatasetItemListResponse)
async def list_dataset_items(
    dataset_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """List individual files (STAC items) that were ingested into this dataset.

    Results are ordered by ``item_datetime`` ascending so the frontend can
    build a chronological timeline.  Only active items are returned.
    """
    from sqlalchemy import select  # noqa: PLC0415

    # Verify the dataset belongs to this org (raises 404 if not)
    service = DatasetService(db)
    await service.get_dataset(dataset_id, organization_id=org_id)

    base_q = (
        select(DatasetItem)
        .where(DatasetItem.dataset_id == dataset_id, DatasetItem.is_active.is_(True))
    )
    total_result = await db.execute(
        base_q.with_only_columns(__import__("sqlalchemy").func.count())
    )
    total = total_result.scalar_one()

    rows = await db.scalars(
        base_q.order_by(DatasetItem.item_datetime.asc().nullslast(), DatasetItem.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return DatasetItemListResponse(
        items=rows.all(), total=total, limit=limit, offset=offset
    )


@router.get("/{dataset_id}/items/{item_id}/tile-config", response_model=DatasetItemTileConfig)
async def get_dataset_item_tile_config(
    dataset_id: UUID,
    item_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return stable tile URL template for a single dataset item (by DB UUID).

    Uses the titiler items endpoint (``/collections/{cid}/items/{iid}/tiles/...``).
    The ``tile_url_template`` points at the API's own tile proxy so the
    browser never needs a direct connection to titiler.  The frontend
    substitutes ``{z}``, ``{x}``, ``{y}`` and may append titiler render
    params (``assets``, ``rescale``, ``colormap``, etc.) as query params.
    """
    return await _build_item_tile_config(db, dataset_id, org_id, item_id=item_id)


@router.get("/{dataset_id}/items/by-stac-id/{stac_item_id}/tile-config", response_model=DatasetItemTileConfig)
async def get_dataset_item_tile_config_by_stac_id(
    dataset_id: UUID,
    stac_item_id: str,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return stable tile URL template for a single dataset item (by STAC item ID string).

    Same as the UUID variant but accepts the STAC item ID string, which is
    what the frontend has when restoring saved map layers from the backend.
    """
    return await _build_item_tile_config(db, dataset_id, org_id, stac_item_id=stac_item_id)


async def _build_item_tile_config(
    db: AsyncSession,
    dataset_id: UUID,
    org_id: UUID,
    *,
    item_id: UUID | None = None,
    stac_item_id: str | None = None,
) -> DatasetItemTileConfig:
    """Shared logic for both tile-config endpoint variants."""
    from app.config import settings as _settings  # noqa: PLC0415

    # Verify dataset ownership
    service = DatasetService(db)
    await service.get_dataset(dataset_id, organization_id=org_id)

    filters = [
        DatasetItem.dataset_id == dataset_id,
        DatasetItem.is_active.is_(True),
    ]
    if item_id is not None:
        filters.append(DatasetItem.id == item_id)
    elif stac_item_id is not None:
        filters.append(DatasetItem.stac_item_id == stac_item_id)
    else:
        raise HTTPException(status_code=400, detail="item_id or stac_item_id required")

    result = await db.execute(select(DatasetItem).where(*filters))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found")

    base = _settings.PUBLIC_API_URL.rstrip("/")
    tile_url_template = (
        f"{base}/api/v1/tiles/collections/{item.stac_collection_id}"
        f"/items/{item.stac_item_id}/{{z}}/{{x}}/{{y}}.png"
    )
    # Include pre-computed rendering config from the item's properties_cache
    rendering_config = (item.properties_cache or {}).get("rendering_config")

    return DatasetItemTileConfig(
        stac_item_id=item.stac_item_id,
        dataset_id=dataset_id,
        tile_url_template=tile_url_template,
        rendering_config=rendering_config,
    )


# ── Segmentation-mask class mapping ───────────────────────────────────────────


@router.get("/{dataset_id}/raster-values", response_model=DatasetRasterValuesRead)
async def get_dataset_raster_values(
    dataset_id: UUID,
    band_index: int = Query(default=1, ge=1),
    max_values: int = Query(default=256, ge=1, le=2048),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Read the unique pixel values of a segmentation mask live from the raster.

    Mirrors the annotation-set raster-mask preview — the value→class mapping UI
    calls this to list the class IDs present in the mask.
    """
    service = DatasetService(db)
    await service.get_dataset(dataset_id, organization_id=org_id)

    item = (
        await db.execute(
            select(DatasetItem)
            .where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.is_active.is_(True),
            )
            .order_by(DatasetItem.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset has no items")

    try:
        async with _raster_preview_semaphore:
            preview = await asyncio.to_thread(
                extract_unique_values, item.s3_uri, _gdal_env_for_api(),
                band_index=band_index, max_values=max_values,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MemoryError:
        raise HTTPException(status_code=507, detail="Raster too large to preview")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read raster values: {exc}") from exc

    return DatasetRasterValuesRead(
        dataset_id=dataset_id,
        dataset_item_id=item.id,
        band_index=band_index,
        values=preview["values"],
        total_unique=preview["total_unique"],
        truncated=preview["truncated"],
    )


@router.patch("/{dataset_id}/class-map", response_model=DatasetClassMapRead)
async def set_dataset_class_map(
    dataset_id: UUID,
    payload: DatasetClassMapUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Associate segmentation-mask pixel values with annotation classes.

    Stores ``{schema_id, value_class_map}`` inside the dataset's
    ``rendering_config`` (and each item's). No colors are stored — the frontend
    derives the overlay colormap from the classes' styles at render time, so the
    overlay self-heals when a class color changes.
    """
    service = DatasetService(db)
    dataset = await service.get_dataset(dataset_id, organization_id=org_id)

    if dataset.dataset_type != DatasetType.SEGMENTATION_MASK.value:
        raise HTTPException(
            status_code=400,
            detail="Class mapping is only valid for segmentation_mask datasets",
        )

    # Schema must exist and belong to the org.
    schema_exists = await db.execute(
        select(AnnotationSchema.id).where(
            AnnotationSchema.id == payload.schema_id,
            AnnotationSchema.organization_id == org_id,
            AnnotationSchema.deleted_at.is_(None),
        )
    )
    if schema_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Annotation schema not found")

    # Every mapped class must belong to the schema.
    class_ids = {UUID(str(v)) for v in payload.value_class_map.values()}
    if class_ids:
        rows = await db.execute(
            select(AnnotationClass.id).where(
                AnnotationClass.schema_id == payload.schema_id,
                AnnotationClass.id.in_(class_ids),
            )
        )
        if len(set(rows.scalars().all())) != len(class_ids):
            raise HTTPException(
                status_code=400,
                detail="One or more class IDs do not belong to the selected schema",
            )

    value_class_map = {
        _normalize_value_key(k): str(v) for k, v in payload.value_class_map.items()
    }
    class_map = {
        "schema_id": str(payload.schema_id),
        "band_index": payload.band_index,
        "nodata_value": payload.nodata_value,
        "value_class_map": value_class_map,
    }

    # Persist into the dataset's rendering_config and every active item's.
    # Reassign the JSONB dicts so SQLAlchemy detects the change.
    meta = dict(dataset.metadata_ or {})
    rc = dict(meta.get("rendering_config") or {})
    rc["class_map"] = class_map
    meta["rendering_config"] = rc
    dataset.metadata_ = meta

    items = (
        await db.execute(
            select(DatasetItem).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.is_active.is_(True),
            )
        )
    ).scalars().all()
    for item in items:
        props = dict(item.properties_cache or {})
        item_rc = dict(props.get("rendering_config") or {})
        item_rc["class_map"] = class_map
        props["rendering_config"] = item_rc
        item.properties_cache = props

    await db.commit()

    await log_audit_event(
        action="datasets.class_map.update", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="dataset", entity_id=str(dataset_id), session=db,
    )

    return DatasetClassMapRead(
        dataset_id=dataset_id,
        schema_id=payload.schema_id,
        band_index=payload.band_index,
        nodata_value=payload.nodata_value,
        value_class_map={k: UUID(v) for k, v in value_class_map.items()},
    )


# ── TiTiler tilejson ──────────────────────────────────────────────────────────


@router.get("/{dataset_id}/tilejson")
async def get_dataset_tilejson(
    dataset_id: UUID,
    assets: str | None = Query(default=None, description="Comma-separated asset names, e.g. B04,B03,B02"),
    rescale: str | None = Query(default=None, description="Min,max rescale range (e.g. '0,10000'). Auto-detected for uint16 data if not provided."),
    asset_bidx: str | None = Query(default=None, description="Band selection (e.g. 'data|1,2,3'). Auto-detected for 4+ band uint16 data if not provided."),
    preset: str | None = Query(default=None, description="Rendering preset name (e.g. 'natural_color', 'ndvi', 'false_color'). Uses default preset if not specified."),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return a TileJSON document for rendering this dataset on Leaflet/MapLibre.

    Uses the titiler collections endpoint (no search registration needed).
    Tile URLs are rewritten to go through the API tile proxy
    (``GET /api/v1/tiles/collections/{cid}/...``) so that auth is enforced
    on every tile request and no additional public port is needed.

    The dataset must have ``status = 'ready'`` (ingestion complete) before
    tiles can be served.

    **Rendering presets**: If the dataset was ingested with rendering metadata,
    the response includes ``rendering.available_presets`` listing all supported
    band combinations (natural_color, ndvi, false_color, etc.). Pass ``preset``
    to switch between them.

    **Auto-rescaling**: For uint16 data, rendering params (rescale, band selection)
    are resolved from cached metadata or auto-detected from titiler. Override
    with explicit ``rescale`` or ``asset_bidx``.
    """
    service = DatasetService(db)
    dataset = await service.get_dataset(dataset_id, organization_id=org_id)

    if dataset.status != DatasetStatus.READY:
        raise HTTPException(
            status_code=409,
            detail=f"Dataset is not ready for tile serving (status: {dataset.status})",
        )
    if not dataset.stac_collection_id:
        raise HTTPException(status_code=409, detail="Dataset has no STAC collection registered")

    # Use pre-computed rendering config from dataset metadata (zero titiler calls)
    rendering_config = (dataset.metadata_ or {}).get("rendering_config")

    try:
        tilejson = await titiler_service.get_collection_tilejson(
            dataset.stac_collection_id,
            assets=assets,
            rendering_config=rendering_config,
            preset=preset,
            rescale=rescale,
            asset_bidx=asset_bidx,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "404" in msg or "not found" in msg.lower() or "not exist" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from exc
        if "timed out" in msg.lower():
            raise HTTPException(status_code=504, detail=msg) from exc
        if "cannot reach" in msg.lower():
            raise HTTPException(status_code=503, detail=msg) from exc
        raise HTTPException(status_code=502, detail=f"Tile service error: {msg}") from exc

    return {
        **tilejson,
        "dataset_id": str(dataset_id),
        "collection_id": dataset.stac_collection_id,
    }

# ── Upload sub-resources ──────────────────────────────────────────────────────


@router.post("/{dataset_id}/uploads/initiate", response_model=UploadInitiateResponse)
async def initiate_dataset_upload(
    dataset_id: UUID,
    payload: UploadInitiateRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Start a multipart upload for a COG file.

    Returns presigned PUT URLs for the first batch of parts. The client
    uploads each part directly to MinIO using those URLs, then calls
    ``complete`` with the resulting ETags.
    """
    # Verify dataset belongs to this org (raises 404 if not)
    service = DatasetService(db)
    await service.get_dataset(dataset_id, organization_id=org_id)

    # Ensure per-org bucket exists (idempotent)
    await asyncio.to_thread(storage_service.ensure_org_bucket, org_id)

    # Initiate MinIO multipart upload
    s3_key, upload_id = await asyncio.to_thread(
        storage_service.initiate_upload, org_id, dataset_id, payload.filename, payload.content_type
    )

    # Create a Job to track the ingestion lifecycle
    job = Job(
        organization_id=org_id,
        type=JobType.INGEST,
        status=JobStatus.PENDING,
        config={
            "s3_key": s3_key,
            "filename": payload.filename,
            "upload_id": upload_id,
        },
        input_refs=[{"type": "dataset", "id": str(dataset_id)}],
        created_by_user_id=current_user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Pre-generate presigned URLs for the first batch of parts.
    # Uses batch generation (single boto3 client) via a thread to avoid
    # blocking the async event loop.
    total_parts = max(1, math.ceil(payload.file_size_bytes / _PART_SIZE_BYTES))
    first_batch = min(_INITIAL_PART_BATCH, total_parts)
    batch_results = await asyncio.to_thread(
        storage_service.generate_part_urls_batch,
        org_id, s3_key, upload_id, list(range(1, first_batch + 1)),
    )
    part_urls = [
        UploadPartUrl(part_number=n, url=url)
        for n, url in batch_results
    ]

    return UploadInitiateResponse(
        upload_id=upload_id,
        job_id=job.id,
        s3_key=s3_key,
        part_size_bytes=_PART_SIZE_BYTES,
        total_parts=total_parts,
        part_urls=part_urls,
    )


@router.post("/{dataset_id}/uploads/{upload_id}/part-urls", response_model=PartUrlsResponse)
async def get_upload_part_urls(
    dataset_id: UUID,
    upload_id: str,
    payload: PartUrlsRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return presigned S3 PUT URLs for additional parts beyond the initial batch."""
    job = await _get_upload_job(db, upload_id, org_id, dataset_id)
    s3_key = job.config["s3_key"]

    batch_results = await asyncio.to_thread(
        storage_service.generate_part_urls_batch,
        org_id, s3_key, upload_id, payload.part_numbers,
    )
    part_urls = [
        UploadPartUrl(part_number=n, url=url)
        for n, url in batch_results
    ]
    return PartUrlsResponse(part_urls=part_urls)


@router.post(
    "/{dataset_id}/uploads/{upload_id}/complete",
    response_model=UploadJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def complete_dataset_upload(
    dataset_id: UUID,
    upload_id: str,
    payload: UploadCompleteRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Finalise the multipart upload and enqueue the ingestion task.

    The client must call this after all parts have been PUT successfully,
    passing the ``part_number`` + ``etag`` pairs returned by MinIO/S3.
    Returns 202 immediately; poll ``GET /jobs/{job_id}`` for progress.
    """
    job = await _get_upload_job(db, upload_id, org_id, dataset_id)
    s3_key = job.config["s3_key"]
    filename = job.config["filename"]

    # MinIO Community doesn't support per-bucket CORS, so browsers cannot read
    # the ETag response header.  Accept an empty/missing parts list and let
    # storage_service.list_parts fetch them server-side.
    boto_parts = (
        [{"PartNumber": p.part_number, "ETag": p.etag} for p in payload.parts]
        if payload.parts
        else None
    )

    try:
        await asyncio.to_thread(
            storage_service.complete_upload, org_id, s3_key, upload_id, boto_parts
        )
    except Exception as exc:
        await asyncio.to_thread(storage_service.abort_upload, org_id, s3_key, upload_id)
        job.status = JobStatus.FAILED
        job.logs = str(exc)
        await db.commit()
        raise HTTPException(status_code=500, detail="Upload completion failed") from exc

    job.status = JobStatus.QUEUED
    await db.commit()

    # Import here to avoid circular imports at module load time
    from app.workers.ingestion.tasks import ingest_dataset  # noqa: PLC0415
    from app.workers.queues import INGESTION  # noqa: PLC0415

    ingest_dataset.apply_async(
        args=[str(job.id), str(dataset_id), s3_key, filename],
        queue=INGESTION,
    )

    return UploadJobResponse(job_id=job.id)


@router.delete(
    "/{dataset_id}/uploads/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def abort_dataset_upload(
    dataset_id: UUID,
    upload_id: str,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Cancel an in-progress multipart upload.

    Calls MinIO's AbortMultipartUpload to release any already-uploaded parts
    and marks the associated Job as ``cancelled``.  Safe to call multiple times.
    """
    job = await _get_upload_job(db, upload_id, org_id, dataset_id)
    s3_key = job.config["s3_key"]

    await asyncio.to_thread(storage_service.abort_upload, org_id, s3_key, upload_id)

    job.status = JobStatus.CANCELLED
    await db.commit()


@router.put(
    "/{dataset_id}/uploads/{upload_id}/parts/{part_number}",
    status_code=status.HTTP_200_OK,
)
async def upload_dataset_part(
    dataset_id: UUID,
    upload_id: str,
    part_number: int,
    request: Request,
):
    """Proxy a single multipart part from the browser to MinIO.

    The browser PUTs raw file bytes to this endpoint.  We forward them to
    MinIO using the internal endpoint, bypassing MinIO Community's missing
    S3 CORS support for direct browser uploads.

    This endpoint is exempt from JWT auth in the middleware — the upload_id
    acts as a capability token (it was issued only to the authenticated user
    who called initiate).  We verify ownership via a BYPASSRLS DB lookup
    (WorkerSession) before forwarding any bytes.  No RLS-scoped session is
    used so the RLS policy cannot block the lookup.

    Part numbers must be in the range 1–10 000 (S3 / MinIO limit).
    Returns 200 with no body on success (ETag is collected server-side at
    complete time via list_parts).
    """
    if not 1 <= part_number <= 10000:
        raise HTTPException(status_code=422, detail="part_number must be 1–10000")

    # Read body FIRST — before any blocking I/O.  Starlette's
    # BaseHTTPMiddleware wraps the ASGI body stream; if we do blocking work
    # (like a DB query via to_thread) before consuming the stream, the
    # middleware's internal Task can mark the stream as disconnected, causing
    # a spurious ClientDisconnect on the subsequent request.body() call.
    try:
        data = await request.body()
    except Exception:
        # Client disconnected before we could read the body — nothing to do.
        return JSONResponse({"detail": "Client disconnected"}, status_code=499)
    if not data:
        raise HTTPException(status_code=422, detail="Request body must not be empty")

    # Verify the upload_id exists and belongs to the given dataset_id.
    # WorkerSession uses the celery_worker DB role (BYPASSRLS) — required
    # because this endpoint is exempt from Clerk auth and no RLS context is set.
    def _lookup() -> tuple[UUID | None, str | None]:
        from app.workers.db import WorkerSession  # noqa: PLC0415
        from sqlalchemy import select as sync_select  # noqa: PLC0415

        with WorkerSession() as session:
            row = session.execute(
                sync_select(Job).where(Job.config["upload_id"].astext == upload_id)
            ).scalar_one_or_none()
            if row is None:
                return None, None
            refs = row.input_refs or []
            if not any(r.get("id") == str(dataset_id) for r in refs):
                return None, None
            return row.organization_id, row.config["s3_key"]

    org_id, s3_key = await asyncio.to_thread(_lookup)
    if org_id is None:
        raise HTTPException(status_code=404, detail="Upload job not found")

    await asyncio.to_thread(
        storage_service.upload_part, org_id, s3_key, upload_id, part_number, data
    )
