import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.ai_model import AIModel
from app.schemas.ai_model import AIModelCreate, AIModelUpdate

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
        query = select(AIModel)
        count_query = select(func.count()).select_from(AIModel)

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
        await self.db.delete(model)
        await self.db.commit()
        logger.info("delete_model_success model_id=%s", model_id)
