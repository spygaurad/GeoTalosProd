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
    """Re-enqueue a failed async job.

    Supports ``ingest`` and ``model_inference`` jobs with ``status = 'failed'``.
    The job is reset to ``pending`` and re-queued; poll this endpoint
    again to track progress.
    """
    service = JobService(db)
    job = await service.get_job(job_id, organization_id=org_id)

    if job.type not in (JobType.INGEST, JobType.MODEL_INFERENCE):
        raise HTTPException(status_code=400, detail="Job type is not retryable")
    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"Job cannot be retried in status '{job.status}' (must be 'failed')",
        )

    # Reset job state
    job.status = JobStatus.PENDING
    job.logs = None
    job.started_at = None
    job.finished_at = None
    await db.commit()
    await db.refresh(job)

    if job.type == JobType.INGEST:
        config = job.config or {}
        s3_key = config.get("s3_key")
        filename = config.get("filename")
        if not s3_key or not filename:
            raise HTTPException(
                status_code=409, detail="Ingest job is missing s3_key or filename in config"
            )
        input_refs = job.input_refs or []
        dataset_ref = next((r for r in input_refs if r.get("type") == "dataset"), None)
        if not dataset_ref:
            raise HTTPException(status_code=409, detail="Ingest job has no dataset reference")
        dataset_id = dataset_ref["id"]
        from app.workers.ingestion.tasks import ingest_dataset  # noqa: PLC0415
        from app.workers.queues import INGESTION  # noqa: PLC0415

        ingest_dataset.apply_async(args=[str(job.id), dataset_id, s3_key, filename], queue=INGESTION)
    elif job.type == JobType.MODEL_INFERENCE:
        from app.workers.inference.tasks import run_batch_inference  # noqa: PLC0415
        from app.workers.queues import INFERENCE  # noqa: PLC0415

        run_batch_inference.apply_async(args=[str(job.id)], queue=INFERENCE)

    return job
