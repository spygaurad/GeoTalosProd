import asyncio
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.exceptions import not_found
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
from app.services.dataset_service import DatasetService
from app.services import storage_service

router = APIRouter(prefix="/datasets", tags=["datasets"])

# Part size used for multipart uploads: 100 MiB.
# MinIO / S3 minimum is 5 MiB per part (except the last).
_PART_SIZE_BYTES = 100 * 1024 * 1024
# Number of presigned part URLs returned in the initiate response.
# Clients needing more call POST /{id}/uploads/{upload_id}/part-urls.
_INITIAL_PART_BATCH = 10


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
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = DatasetService(db)
    dataset = await service.create_dataset(payload)
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
        storage_service.initiate_upload, org_id, dataset_id, payload.filename
    )

    # Create a Job to track the ingestion lifecycle
    job = Job(
        organization_id=org_id,
        type="ingest",
        status="pending",
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

    # Pre-generate the first batch of presigned part URLs
    total_parts = max(1, math.ceil(payload.file_size_bytes / _PART_SIZE_BYTES))
    first_batch = min(_INITIAL_PART_BATCH, total_parts)
    part_urls = []
    for part_number in range(1, first_batch + 1):
        url = await asyncio.to_thread(
            storage_service.generate_part_url, org_id, s3_key, upload_id, part_number
        )
        part_urls.append(UploadPartUrl(part_number=part_number, url=url))

    return UploadInitiateResponse(
        upload_id=upload_id,
        job_id=job.id,
        s3_key=s3_key,
        part_size_bytes=_PART_SIZE_BYTES,
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
    """Return presigned PUT URLs for additional parts beyond the initial batch."""
    job = await _get_upload_job(db, upload_id, org_id, dataset_id)
    s3_key = job.config["s3_key"]

    part_urls = []
    for part_number in payload.part_numbers:
        url = await asyncio.to_thread(
            storage_service.generate_part_url, org_id, s3_key, upload_id, part_number
        )
        part_urls.append(UploadPartUrl(part_number=part_number, url=url))

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

    boto_parts = [
        {"PartNumber": p.part_number, "ETag": p.etag} for p in payload.parts
    ]

    try:
        await asyncio.to_thread(
            storage_service.complete_upload, org_id, s3_key, upload_id, boto_parts
        )
    except Exception as exc:
        await asyncio.to_thread(storage_service.abort_upload, org_id, s3_key, upload_id)
        job.status = "failed"
        job.logs = str(exc)
        await db.commit()
        raise HTTPException(status_code=500, detail="Upload completion failed") from exc

    job.status = "queued"
    await db.commit()

    # Import here to avoid circular imports at module load time
    from app.workers.ingestion.tasks import ingest_dataset  # noqa: PLC0415

    ingest_dataset.apply_async(args=[str(job.id), str(dataset_id), s3_key, filename])

    return UploadJobResponse(job_id=job.id)
