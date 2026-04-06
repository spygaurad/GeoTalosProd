from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.models.ai_model import AIModel
from app.models.dataset_item import DatasetItem
from app.models.map import Map
from app.models.project import Project
from app.models.job import Job
from app.models.user import User
from app.schemas.job import InferenceJobCreate, JobRead
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/inference", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def create_inference_job(
    payload: InferenceJobCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    model = await db.scalar(
        select(AIModel).where(
            AIModel.id == payload.model_id,
            AIModel.organization_id == org_id,
            AIModel.deleted_at.is_(None),
        )
    )
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")

    rows = await db.scalars(
        select(DatasetItem).where(
            DatasetItem.id.in_(payload.dataset_item_ids),
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    found = rows.all()
    if len(found) != len(set(payload.dataset_item_ids)):
        raise HTTPException(status_code=404, detail="One or more dataset items were not found")

    if payload.project_id is not None:
        project = await db.scalar(
            select(Project).where(
                Project.id == payload.project_id,
                Project.organization_id == org_id,
                Project.deleted_at.is_(None),
            )
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

    if payload.map_id is not None:
        map_row = await db.scalar(
            select(Map)
            .join(Project, Project.id == Map.project_id)
            .where(
                Map.id == payload.map_id,
                Project.organization_id == org_id,
                Map.deleted_at.is_(None),
            )
        )
        if map_row is None:
            raise HTTPException(status_code=404, detail="Map not found")

    run_output_config = dict(model.output_config or {})
    run_output_config.update(
        {
            "project_id": str(payload.project_id) if payload.project_id else None,
            "map_id": str(payload.map_id) if payload.map_id else None,
            "mount_on_map": payload.mount_on_map,
        }
    )

    job = Job(
        organization_id=org_id,
        type=JobType.INFERENCE,
        status=JobStatus.QUEUED,
        config={"trigger": "api", "run_output_config": run_output_config},
        input_refs=[{"type": "dataset_item", "id": str(item_id)} for item_id in payload.dataset_item_ids],
        created_by_user_id=current_user.id,
        model_id=payload.model_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from app.workers.inference.tasks import run_inference_batch  # noqa: PLC0415

    run_inference_batch.apply_async(args=[str(job.id)])
    return job


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
