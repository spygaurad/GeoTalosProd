from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.core.exceptions import bad_request
from app.models.job import Job
from app.models.user import User
from app.schemas.job import JobRead

from geoops.yolo_schemas import (
    YoloExportPreviewRequest,
    YoloExportPreviewResponse,
    YoloFinetuneRequest,
)
from geoops.yolo_service import YoloExportService

router = APIRouter(prefix="/yolo", tags=["yolo"])


@router.post("/export-preview", response_model=YoloExportPreviewResponse)
async def export_preview(
    payload: YoloExportPreviewRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Validate + preview the training corpus an AnnotationSetCollection would
    export to, without calling TiTiler, S3, or yolo-service.

    Safe to call repeatedly while deciding what to train on: reports which
    member sets are eligible (verified + schema-matched) vs skipped and why,
    per-class annotation/image counts, and whether there's anything to train
    on at all (``ready``).
    """
    service = YoloExportService(db)
    return await service.build_preview(org_id, payload.annotation_set_collection_id)


@router.post("/finetune", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def finetune(
    payload: YoloFinetuneRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Export a verified AnnotationSetCollection to a YOLO training dataset (async job).

    Validates the collection exists and has at least one eligible annotation
    before enqueueing — run ``POST /yolo/export-preview`` first if you want
    to inspect what will be exported without waiting on the job. Poll
    ``GET /api/jobs/{job_id}`` for ``config.result`` once ``status ==
    "completed"``.
    """
    service = YoloExportService(db)
    preview = await service.build_preview(org_id, payload.annotation_set_collection_id)
    if not preview.ready:
        raise bad_request(
            "Nothing eligible to export: 0 verified, resolvable-item annotations in this collection."
        )

    job = Job(
        organization_id=org_id,
        type=JobType.FINETUNE_MODEL,
        status=JobStatus.QUEUED,
        config={"request": payload.model_dump(mode="json")},
        input_refs=[
            {"type": "annotation_set_collection", "id": str(payload.annotation_set_collection_id)}
        ],
        created_by_user_id=current_user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    from geoops.yolo_tasks import run_finetune_export  # noqa: PLC0415

    run_finetune_export.apply_async(args=[str(job.id)])
    return job
