import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus, JobType
from app.core.exceptions import bad_request, not_found
from app.models.ai_model import AIModel
from app.models.annotation_schema import AnnotationSchema
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.map import Map
from app.models.project import Project
from app.schemas.inference import InferenceBatchCreate
from app.workers.queues import INFERENCE

logger = logging.getLogger(__name__)


class InferenceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _require_map_for_org(self, map_id: UUID, organization_id: UUID) -> Map:
        result = await self.db.execute(
            select(Map)
            .join(Project, Project.id == Map.project_id)
            .where(
                Map.id == map_id,
                Map.deleted_at.is_(None),
                Project.organization_id == organization_id,
            )
        )
        map_row = result.scalar_one_or_none()
        if map_row is None:
            raise not_found("Map")
        return map_row

    async def _require_model_for_org(self, model_id: UUID, organization_id: UUID) -> AIModel:
        result = await self.db.execute(
            select(AIModel).where(
                AIModel.id == model_id,
                AIModel.organization_id == organization_id,
                AIModel.deleted_at.is_(None),
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            raise not_found("Model")
        return model

    async def _require_schema_for_org(
        self, schema_id: UUID, organization_id: UUID
    ) -> AnnotationSchema:
        result = await self.db.execute(
            select(AnnotationSchema).where(
                AnnotationSchema.id == schema_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        schema = result.scalar_one_or_none()
        if schema is None:
            raise not_found("AnnotationSchema")
        return schema

    async def _dataset_item_count(
        self, dataset_id: UUID, organization_id: UUID
    ) -> int:
        dataset_result = await self.db.execute(
            select(Dataset.id).where(
                Dataset.id == dataset_id,
                Dataset.organization_id == organization_id,
                Dataset.deleted_at.is_(None),
            )
        )
        if dataset_result.scalar_one_or_none() is None:
            raise not_found("Dataset")

        result = await self.db.execute(
            select(func.count()).select_from(DatasetItem).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.organization_id == organization_id,
                DatasetItem.is_active.is_(True),
            )
        )
        count = int(result.scalar() or 0)
        if count == 0:
            raise bad_request("Dataset has no active STAC items for inference")
        return count

    async def create_batch_inference_job(
        self,
        payload: InferenceBatchCreate,
        organization_id: UUID,
        created_by_user_id: UUID,
    ) -> Job:
        await self._require_map_for_org(payload.map_id, organization_id)
        await self._require_model_for_org(payload.model_id, organization_id)
        await self._require_schema_for_org(payload.schema_id, organization_id)

        if payload.dataset_id is not None:
            total_items = await self._dataset_item_count(payload.dataset_id, organization_id)
            input_refs = [{"type": "dataset", "id": str(payload.dataset_id)}]
        else:
            stac_item_ids = [item for item in (payload.stac_item_ids or []) if item]
            if not stac_item_ids:
                raise bad_request("stac_item_ids cannot be empty")
            total_items = len(stac_item_ids)
            input_refs = [{"type": "stac_item", "id": item_id} for item_id in stac_item_ids]

        config = {
            "map_id": str(payload.map_id),
            "schema_id": str(payload.schema_id),
            "dataset_id": str(payload.dataset_id) if payload.dataset_id else None,
            "stac_item_ids": payload.stac_item_ids or [],
            "params": payload.params or {},
            "set_name": payload.set_name,
            "create_overlay_layer": payload.create_overlay_layer,
            "auto_create_classes": payload.auto_create_classes,
        }

        job = Job(
            organization_id=organization_id,
            type=JobType.MODEL_INFERENCE,
            status=JobStatus.QUEUED,
            config=config,
            input_refs=input_refs,
            total_items=total_items,
            created_by_user_id=created_by_user_id,
            model_id=payload.model_id,
        )
        self.db.add(job)
        await self.db.commit()
        await self.db.refresh(job)

        from app.workers.inference.tasks import run_batch_inference  # noqa: PLC0415

        run_batch_inference.apply_async(args=[str(job.id)], queue=INFERENCE)
        logger.info("inference_job_queued job_id=%s total_items=%s", job.id, total_items)
        return job
