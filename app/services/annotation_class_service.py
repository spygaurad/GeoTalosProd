import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import conflict, not_found
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.style import Style
from app.schemas.annotation_class import (
    AnnotationClassCreate,
    AnnotationClassUpdate,
    ClassStyleUpsert,
)

logger = logging.getLogger(__name__)


class AnnotationClassService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_schema_for_org(
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

    async def _get_class_for_org(
        self, class_id: UUID, organization_id: UUID
    ) -> AnnotationClass:
        """Load a class with its style eagerly."""
        result = await self.db.execute(
            select(AnnotationClass)
            .options(selectinload(AnnotationClass.style))
            .join(AnnotationSchema, AnnotationSchema.id == AnnotationClass.schema_id)
            .where(
                AnnotationClass.id == class_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        cls = result.scalar_one_or_none()
        if cls is None:
            raise not_found("AnnotationClass")
        return cls

    async def list_classes(
        self,
        schema_id: UUID,
        limit: int,
        offset: int,
        organization_id: UUID,
    ) -> tuple[Sequence[AnnotationClass], int]:
        await self._get_schema_for_org(schema_id, organization_id)
        query = (
            select(AnnotationClass)
            .options(selectinload(AnnotationClass.style))
            .where(AnnotationClass.schema_id == schema_id)
        )
        count_query = (
            select(func.count())
            .select_from(AnnotationClass)
            .where(AnnotationClass.schema_id == schema_id)
        )
        rows = await self.db.scalars(
            query.order_by(AnnotationClass.created_at.asc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_class(
        self, class_id: UUID, organization_id: UUID
    ) -> AnnotationClass:
        return await self._get_class_for_org(class_id, organization_id)

    async def create_class(
        self,
        schema_id: UUID,
        payload: AnnotationClassCreate,
        organization_id: UUID,
    ) -> AnnotationClass:
        await self._get_schema_for_org(schema_id, organization_id)
        data = payload.model_dump()
        data["schema_id"] = schema_id
        cls = AnnotationClass(**data)
        self.db.add(cls)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_class_conflict schema_id=%s", schema_id)
            raise conflict("Annotation class violates constraints") from exc
        await self.db.refresh(cls)
        # Reload with style relationship
        return await self._get_class_for_org(cls.id, organization_id)

    async def update_class(
        self,
        class_id: UUID,
        payload: AnnotationClassUpdate,
        organization_id: UUID,
    ) -> AnnotationClass:
        cls = await self._get_class_for_org(class_id, organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(cls, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation class update violates constraints") from exc
        return await self._get_class_for_org(cls.id, organization_id)

    async def upsert_style(
        self,
        schema_id: UUID,
        class_id: UUID,
        payload: ClassStyleUpsert,
        organization_id: UUID,
    ) -> AnnotationClass:
        """
        Create or update the Style linked to a class.

        • If the class has no style_id → create a new Style, link it.
        • If the class already has a style_id → patch the existing Style in-place.
        """
        cls = await self._get_class_for_org(class_id, organization_id)

        style_name = payload.name or f"{cls.name} style"
        style_type = payload.type or "polygon"

        if cls.style_id is None:
            # Create a brand-new Style record and link it
            new_style = Style(
                organization_id=organization_id,
                name=style_name,
                type=style_type,
                definition=payload.definition,
            )
            self.db.add(new_style)
            await self.db.flush()          # get new_style.id without full commit
            cls.style_id = new_style.id
        else:
            # Update the existing Style definition in-place
            result = await self.db.execute(
                select(Style).where(Style.id == cls.style_id)
            )
            existing_style = result.scalar_one_or_none()
            if existing_style is None:
                # Style was deleted externally — create fresh
                new_style = Style(
                    organization_id=organization_id,
                    name=style_name,
                    type=style_type,
                    definition=payload.definition,
                )
                self.db.add(new_style)
                await self.db.flush()
                cls.style_id = new_style.id
            else:
                # Merge definition — keep existing keys not in payload
                merged = {**existing_style.definition, **payload.definition}
                existing_style.definition = merged
                if payload.name:
                    existing_style.name = payload.name
                if payload.type:
                    existing_style.type = payload.type

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Style upsert violates constraints") from exc

        return await self._get_class_for_org(class_id, organization_id)

    async def delete_class(self, class_id: UUID, organization_id: UUID) -> None:
        cls = await self._get_class_for_org(class_id, organization_id)
        await self.db.delete(cls)
        await self.db.commit()
