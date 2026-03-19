import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.annotation_schema import AnnotationSchema
from app.schemas.annotation_schema import AnnotationSchemaCreate, AnnotationSchemaUpdate

logger = logging.getLogger(__name__)


class AnnotationSchemaService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_schemas(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[AnnotationSchema], int]:
        query = select(AnnotationSchema).where(AnnotationSchema.deleted_at.is_(None))
        count_query = (
            select(func.count()).select_from(AnnotationSchema).where(AnnotationSchema.deleted_at.is_(None))
        )
        if organization_id is not None:
            query = query.where(AnnotationSchema.organization_id == organization_id)
            count_query = count_query.where(AnnotationSchema.organization_id == organization_id)

        rows = await self.db.scalars(
            query.order_by(AnnotationSchema.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_schema(
        self, schema_id: UUID, organization_id: UUID | None = None
    ) -> AnnotationSchema:
        query = select(AnnotationSchema).where(
            AnnotationSchema.id == schema_id, AnnotationSchema.deleted_at.is_(None)
        )
        if organization_id is not None:
            query = query.where(AnnotationSchema.organization_id == organization_id)
        result = await self.db.execute(query)
        schema = result.scalar_one_or_none()
        if schema is None:
            raise not_found("AnnotationSchema")
        return schema

    async def create_schema(
        self,
        payload: AnnotationSchemaCreate,
        organization_id: UUID,
        created_by: UUID | None = None,
    ) -> AnnotationSchema:
        data = payload.model_dump()
        data["organization_id"] = organization_id
        data["created_by"] = created_by
        schema = AnnotationSchema(**data)
        self.db.add(schema)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_schema_conflict organization_id=%s", organization_id)
            raise conflict("Annotation schema violates uniqueness or FK constraints") from exc
        await self.db.refresh(schema)
        return schema

    async def update_schema(
        self,
        schema_id: UUID,
        payload: AnnotationSchemaUpdate,
        organization_id: UUID | None = None,
    ) -> AnnotationSchema:
        schema = await self.get_schema(schema_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(schema, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation schema update violates constraints") from exc
        await self.db.refresh(schema)
        return schema

    async def delete_schema(
        self, schema_id: UUID, organization_id: UUID | None = None
    ) -> None:
        schema = await self.get_schema(schema_id, organization_id=organization_id)
        schema.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
