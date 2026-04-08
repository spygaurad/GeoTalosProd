import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.ai_model import AIModel
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.model_class_mapping import ModelClassMapping
from app.schemas.ai_model import AIModelCreate, AIModelUpdate, ModelClassMappingCreate

logger = logging.getLogger(__name__)


class AIModelService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_models(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[AIModel], int]:
        query = select(AIModel).where(AIModel.deleted_at.is_(None))
        count_query = select(func.count()).select_from(AIModel).where(AIModel.deleted_at.is_(None))

        if organization_id is not None:
            query = query.where(AIModel.organization_id == organization_id)
            count_query = count_query.where(AIModel.organization_id == organization_id)

        rows = await self.db.scalars(
            query.order_by(AIModel.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_models organization_id=%s limit=%s offset=%s total=%s",
            organization_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_model(self, model_id: UUID, organization_id: UUID | None = None) -> AIModel:
        if organization_id is None:
            model = await self.db.get(AIModel, model_id)
        else:
            result = await self.db.execute(
                select(AIModel).where(
                    AIModel.id == model_id, AIModel.organization_id == organization_id
                )
            )
            model = result.scalar_one_or_none()
        if model is None:
            logger.warning("get_model_not_found model_id=%s", model_id)
            raise not_found("Model")
        return model

    async def create_model(self, payload: AIModelCreate) -> AIModel:
        if payload.annotation_schema_id is not None:
            result = await self.db.execute(
                select(AnnotationSchema.id).where(
                    AnnotationSchema.id == payload.annotation_schema_id,
                    AnnotationSchema.organization_id == payload.organization_id,
                    AnnotationSchema.deleted_at.is_(None),
                )
            )
            if result.scalar_one_or_none() is None:
                raise not_found("AnnotationSchema")
        model = AIModel(**payload.model_dump())
        self.db.add(model)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_model_conflict organization_id=%s", payload.organization_id)
            raise conflict("Model creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(model)
        logger.info("create_model_success model_id=%s", model.id)
        return model

    async def update_model(
        self, model_id: UUID, payload: AIModelUpdate, organization_id: UUID | None = None
    ) -> AIModel:
        model = await self.get_model(model_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)
        if "annotation_schema_id" in data and data["annotation_schema_id"] is not None:
            if organization_id is None:
                raise conflict("organization_id is required for annotation schema binding")
            result = await self.db.execute(
                select(AnnotationSchema.id).where(
                    AnnotationSchema.id == data["annotation_schema_id"],
                    AnnotationSchema.organization_id == organization_id,
                    AnnotationSchema.deleted_at.is_(None),
                )
            )
            if result.scalar_one_or_none() is None:
                raise not_found("AnnotationSchema")

        for key, value in data.items():
            setattr(model, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_model_conflict model_id=%s", model_id)
            raise conflict("Model update violates uniqueness or FK constraints") from exc
        await self.db.refresh(model)
        logger.info("update_model_success model_id=%s", model.id)
        return model

    async def delete_model(self, model_id: UUID, organization_id: UUID | None = None) -> None:
        model = await self.get_model(model_id, organization_id=organization_id)
        model.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
        logger.info("delete_model_success model_id=%s", model_id)

    async def list_class_mappings(
        self, model_id: UUID, organization_id: UUID
    ) -> list[ModelClassMapping]:
        await self.get_model(model_id, organization_id=organization_id)
        rows = await self.db.scalars(
            select(ModelClassMapping)
            .where(ModelClassMapping.model_id == model_id)
            .order_by(ModelClassMapping.priority.desc(), ModelClassMapping.model_label.asc())
        )
        return rows.all()

    async def _validate_mapping_class(
        self, annotation_class_id: UUID, model: AIModel, organization_id: UUID
    ) -> None:
        result = await self.db.execute(
            select(AnnotationClass.id)
            .join(AnnotationSchema, AnnotationSchema.id == AnnotationClass.schema_id)
            .where(
                AnnotationClass.id == annotation_class_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("AnnotationClass")
        if model.annotation_schema_id is not None:
            result = await self.db.execute(
                select(AnnotationClass.id).where(
                    AnnotationClass.id == annotation_class_id,
                    AnnotationClass.schema_id == model.annotation_schema_id,
                )
            )
            if result.scalar_one_or_none() is None:
                raise conflict("annotation_class_id is not part of model.annotation_schema_id")

    async def upsert_class_mappings(
        self,
        model_id: UUID,
        payload: list[ModelClassMappingCreate],
        organization_id: UUID,
    ) -> list[ModelClassMapping]:
        model = await self.get_model(model_id, organization_id=organization_id)
        created: list[ModelClassMapping] = []
        for item in payload:
            await self._validate_mapping_class(item.annotation_class_id, model, organization_id)
            existing = await self.db.scalar(
                select(ModelClassMapping).where(
                    ModelClassMapping.model_id == model_id,
                    ModelClassMapping.model_label == item.model_label,
                )
            )
            if existing is None:
                existing = ModelClassMapping(model_id=model_id, **item.model_dump())
                self.db.add(existing)
            else:
                existing.annotation_class_id = item.annotation_class_id
                existing.confidence_threshold = item.confidence_threshold
                existing.priority = item.priority
            created.append(existing)
        await self.db.commit()
        for mapping in created:
            await self.db.refresh(mapping)
        return created

    async def replace_class_mappings(
        self,
        model_id: UUID,
        payload: list[ModelClassMappingCreate],
        organization_id: UUID,
    ) -> list[ModelClassMapping]:
        await self.get_model(model_id, organization_id=organization_id)
        existing_rows = await self.db.scalars(
            select(ModelClassMapping).where(ModelClassMapping.model_id == model_id)
        )
        for row in existing_rows:
            await self.db.delete(row)
        await self.db.flush()
        return await self.upsert_class_mappings(model_id, payload, organization_id)

    async def delete_class_mapping(
        self, model_id: UUID, mapping_id: UUID, organization_id: UUID
    ) -> None:
        await self.get_model(model_id, organization_id=organization_id)
        mapping = await self.db.scalar(
            select(ModelClassMapping).where(
                ModelClassMapping.id == mapping_id,
                ModelClassMapping.model_id == model_id,
            )
        )
        if mapping is None:
            raise not_found("ModelClassMapping")
        await self.db.delete(mapping)
        await self.db.commit()
