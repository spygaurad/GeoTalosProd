from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.core.exceptions import bad_request
from app.models.job import Job
from app.models.user import User
from app.schemas.job import JobRead

from geoops.dino_schemas import (
    DinoExportPreviewRequest,
    DinoExportPreviewResponse,
    DinoFinetuneRequest,
)
from geoops.dino_service import DinoExportService

router = APIRouter(prefix="/dino", tags=["dino"])


@router.post("/export-preview", response_model=DinoExportPreviewResponse)
async def export_preview(
    payload: DinoExportPreviewRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Validate + preview the training corpus for ONE class-specific head.

    Scoped to a single class_id, unlike /yolo/export-preview which is
    collection-wide — the DINOv2 tier trains one head per class.
    """
    service = DinoExportService(db)
    return await service.build_preview(org_id, payload.annotation_set_collection_id, payload.class_id)


@router.post("/finetune", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def finetune(
    payload: DinoFinetuneRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Export training data for one class-specific head (async job).

    Run ``POST /dino/export-preview`` first if you want to inspect what
    will be exported without waiting on the job. Poll
    ``GET /api/jobs/{job_id}`` for ``config.result`` once ``status ==
    "completed"``.
    """
    service = DinoExportService(db)
    preview = await service.build_preview(org_id, payload.annotation_set_collection_id, payload.class_id)
    if not preview.ready:
        raise bad_request(
            "Nothing eligible to export: 0 verified, resolvable-item annotations of this class."
        )

    job = Job(
        organization_id=org_id,
        type=JobType.FINETUNE_MODEL,
        status=JobStatus.QUEUED,
        config={"request": payload.model_dump(mode="json"), "tier": "dino_head"},
        input_refs=[
            {"type": "annotation_set_collection", "id": str(payload.annotation_set_collection_id)},
            {"type": "annotation_class", "id": str(payload.class_id)},
        ],
        created_by_user_id=current_user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from geoops.dino_tasks import run_dino_finetune_export  # noqa: PLC0415

    run_dino_finetune_export.apply_async(args=[str(job.id)])
    return job
