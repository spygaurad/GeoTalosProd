import asyncio
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.enums import DatasetStatus, JobStatus, JobType
from app.core.exceptions import not_found
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User
from app.schemas.dataset import (
    DatasetCreate,
    DatasetListResponse,
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

router = APIRouter(prefix="/datasets", tags=["datasets"])

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
    """Return stable tile URL template for a single dataset item.

    The ``tile_url_template`` points at the API's own tile proxy so the
    browser never needs a direct connection to titiler.  The frontend
    substitutes ``{z}``, ``{x}``, ``{y}`` and may append titiler render
    params (``assets``, ``rescale``, ``colormap``, etc.) as query params.
    """
    from sqlalchemy import select  # noqa: PLC0415
    from app.config import settings as _settings  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    # Verify dataset ownership
    service = DatasetService(db)
    await service.get_dataset(dataset_id, organization_id=org_id)

    result = await db.execute(
        select(DatasetItem).where(
            DatasetItem.id == item_id,
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.is_active.is_(True),
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found")

    base = _settings.PUBLIC_API_URL.rstrip("/")
    encoded_uri = urllib.parse.quote(item.s3_uri, safe="")
    tile_url_template = (
        f"{base}/api/v1/tiles/stac/{{z}}/{{x}}/{{y}}.png?url={encoded_uri}"
    )
    return DatasetItemTileConfig(
        stac_item_id=item.stac_item_id,
        dataset_id=dataset_id,
        tile_url_template=tile_url_template,
    )


# ── TiTiler tilejson ──────────────────────────────────────────────────────────


@router.get("/{dataset_id}/tilejson")
async def get_dataset_tilejson(
    dataset_id: UUID,
    assets: str | None = Query(default=None, description="Comma-separated asset names, e.g. B04,B03,B02"),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return a TileJSON document for rendering this dataset on Leaflet/MapLibre.

    Tile URLs in the response are rewritten to go through the API tile proxy
    (``GET /api/v1/tiles/mosaic/{searchid}/...``) so that auth is enforced on
    every tile request and no additional public port is needed.

    The dataset must have ``status = 'ready'`` (ingestion complete) before
    tiles can be served.
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

    try:
        searchid = await titiler_service.register_collection_mosaic(dataset.stac_collection_id)
        tilejson = await titiler_service.get_mosaic_tilejson(
            searchid,
            assets=assets,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "not found" in msg.lower() or "no items" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from exc
        if "timed out" in msg.lower():
            raise HTTPException(status_code=504, detail=msg) from exc
        if "cannot reach" in msg.lower():
            raise HTTPException(status_code=503, detail=msg) from exc
        raise HTTPException(status_code=502, detail=f"Tile service error: {msg}") from exc

    return {**tilejson, "dataset_id": str(dataset_id), "searchid": searchid}

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
