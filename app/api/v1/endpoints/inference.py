"""Direct ML inference endpoints (bypass automation pipeline).

POST /inference/sam3 — run SAM3 PCS or PVS against a dataset item, returning
a job_id + annotation_set_id. Client polls GET /jobs/{id} for progress.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from shapely.geometry import shape as shp_shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.enums import JobStatus, JobType
from app.models.ai_model import AIModel
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User
from app.schemas.inference import SAM3InferenceRequest, SAM3InferenceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inference", tags=["inference"])


@router.post(
    "/sam3",
    response_model=SAM3InferenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_sam3_inference(
    payload: SAM3InferenceRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SAM3InferenceResponse:
    """Start a SAM3 inference job. Returns 202 with job_id + annotation_set_id."""
    # 1. Validate model is in org
    model_result = await db.execute(
        select(AIModel).where(
            AIModel.id == payload.model_id,
            AIModel.organization_id == org_id,
            AIModel.deleted_at.is_(None),
        )
    )
    ai_model = model_result.scalar_one_or_none()
    if ai_model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    if not ai_model.endpoint_url:
        raise HTTPException(
            status_code=400,
            detail="Model has no endpoint_url configured",
        )
    if ai_model.annotation_schema_id is None:
        # Stage 1 of the unified platform plan: every annotation set must carry
        # a schema. Fail fast rather than hitting the DB NOT NULL on insert.
        raise HTTPException(
            status_code=400,
            detail="Model has no annotation_schema_id configured",
        )

    # 2. Validate dataset_item is in org
    item_result = await db.execute(
        select(DatasetItem).where(
            DatasetItem.id == payload.dataset_item_id,
            DatasetItem.organization_id == org_id,
        )
    )
    dataset_item = item_result.scalar_one_or_none()
    if dataset_item is None:
        raise HTTPException(status_code=404, detail="DatasetItem not found")

    # 3. Validate AOI geometry if provided
    if payload.aoi_geometry is not None:
        try:
            aoi_shape = shp_shape(payload.aoi_geometry)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid aoi_geometry: {exc}"
            ) from exc
        if aoi_shape.geom_type not in ("Polygon", "MultiPolygon"):
            raise HTTPException(
                status_code=400,
                detail=f"aoi_geometry must be Polygon or MultiPolygon, got {aoi_shape.geom_type}",
            )

    # 4. Create Job first (AnnotationSet CHECK requires job_id or created_by_user_id)
    cfg = payload.model_dump(mode="json")
    job = Job(
        organization_id=org_id,
        type=JobType.INFERENCE,
        status=JobStatus.PENDING,
        config=cfg,
        model_id=ai_model.id,
        total_items=1,
        created_by_user_id=current_user.id,
    )
    db.add(job)
    await db.flush()

    # 5. Create AnnotationSet linked to the job
    annotation_set = AnnotationSet(
        organization_id=org_id,
        name=payload.annotation_set_name,
        source_type="model",
        model_id=ai_model.id,
        job_id=job.id,
        schema_id=ai_model.annotation_schema_id,
        dataset_item_id=dataset_item.id,
        dataset_id=dataset_item.dataset_id,
    )
    db.add(annotation_set)
    await db.flush()

    # 6. Update job.config with the annotation_set_id so the worker can find it
    cfg["annotation_set_id"] = str(annotation_set.id)
    job.config = cfg

    await db.commit()
    await db.refresh(job)
    await db.refresh(annotation_set)

    # 7. Enqueue the Celery task
    from app.workers.inference.tasks import run_inference_job  # noqa: PLC0415
    run_inference_job.apply_async(args=[str(job.id)])

    logger.info(
        "sam3_inference_enqueued job_id=%s annotation_set_id=%s model_id=%s",
        job.id, annotation_set.id, ai_model.id,
    )

    return SAM3InferenceResponse(
        job_id=job.id,
        annotation_set_id=annotation_set.id,
        status=job.status,
    )
