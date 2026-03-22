from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.models.user import User
from app.schemas.inference import InferenceBatchCreate, InferenceBatchJobResponse
from app.services.inference_service import InferenceService
from app.services.job_service import JobService

router = APIRouter(prefix="/inference", tags=["inference"])


@router.post("/jobs/batch", response_model=InferenceBatchJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_batch_inference_job(
    payload: InferenceBatchCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = InferenceService(db)
    job = await service.create_batch_inference_job(
        payload=payload,
        organization_id=org_id,
        created_by_user_id=current_user.id,
    )
    await log_audit_event(
        action="inference.jobs.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="job",
        entity_id=str(job.id),
        session=db,
    )

    job_service = JobService(db)
    refreshed = await job_service.get_job(job.id, organization_id=org_id)
    return InferenceBatchJobResponse(job=refreshed)
