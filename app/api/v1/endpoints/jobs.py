from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.models.user import User
from app.schemas.job import JobRead
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Poll the status of an async job.

    Status lifecycle: pending → queued → running → completed / failed.
    """
    service = JobService(db)
    return await service.get_job(job_id, organization_id=org_id)


@router.post("/{job_id}/retry", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def retry_job(
    job_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Re-enqueue a failed ingestion job without re-uploading the file.

    Only ``ingest`` jobs with ``status = 'failed'`` can be retried.
    The job is reset to ``pending`` and re-queued; poll this endpoint
    again to track progress.
    """
    service = JobService(db)
    job = await service.get_job(job_id, organization_id=org_id)

    if job.type != JobType.INGEST:
        raise HTTPException(
            status_code=400, detail="Only ingest jobs can be retried via this endpoint"
        )
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"Job cannot be retried in status '{job.status}' (must be 'failed')",
        )

    config = job.config or {}
    s3_key = config.get("s3_key")
    filename = config.get("filename")
    if not s3_key or not filename:
        raise HTTPException(
            status_code=409, detail="Job is missing s3_key or filename in config"
        )

    # Find the dataset_id from input_refs
    input_refs = job.input_refs or []
    dataset_ref = next((r for r in input_refs if r.get("type") == "dataset"), None)
    if not dataset_ref:
        raise HTTPException(status_code=409, detail="Job has no dataset reference")
    dataset_id = dataset_ref["id"]

    # Reset job state
    job.status = JobStatus.PENDING
    job.logs = None
    job.started_at = None
    job.finished_at = None
    await db.commit()
    await db.refresh(job)

    # Re-enqueue the Celery task
    from app.workers.ingestion.tasks import ingest_dataset  # noqa: PLC0415

    ingest_dataset.apply_async(args=[str(job.id), dataset_id, s3_key, filename])

    return job
