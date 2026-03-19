import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.dataset import Dataset
from app.models.map import Map
from app.models.project import Project
from app.schemas.annotation_set import AnnotationSetCreate, AnnotationSetUpdate

logger = logging.getLogger(__name__)


class AnnotationSetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_map_for_org(self, map_id: UUID, organization_id: UUID) -> Map:
        result = await self.db.execute(
            select(Map)
            .join(Project, Project.id == Map.project_id)
            .where(Map.id == map_id, Map.deleted_at.is_(None))
            .where(Project.organization_id == organization_id)
        )
        map_row = result.scalar_one_or_none()
        if map_row is None:
            raise not_found("Map")
        return map_row

    async def _require_schema_for_org(
        self, schema_id: UUID, organization_id: UUID
    ) -> None:
        result = await self.db.execute(
            select(AnnotationSchema.id).where(
                AnnotationSchema.id == schema_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("AnnotationSchema")

    async def _require_dataset_for_org(
        self, dataset_id: UUID, organization_id: UUID
    ) -> None:
        result = await self.db.execute(
            select(Dataset.id).where(
                Dataset.id == dataset_id,
                Dataset.organization_id == organization_id,
                Dataset.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("Dataset")

    async def list_sets(
        self,
        map_id: UUID,
        limit: int,
        offset: int,
        organization_id: UUID,
    ) -> tuple[Sequence[AnnotationSet], int]:
        await self._get_map_for_org(map_id, organization_id)
        query = select(AnnotationSet).where(
            AnnotationSet.map_id == map_id,
            AnnotationSet.deleted_at.is_(None),
        )
        count_query = (
            select(func.count())
            .select_from(AnnotationSet)
            .where(AnnotationSet.map_id == map_id, AnnotationSet.deleted_at.is_(None))
        )
        rows = await self.db.scalars(
            query.order_by(AnnotationSet.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_set(
        self, set_id: UUID, organization_id: UUID, map_id: UUID | None = None
    ) -> AnnotationSet:
        query = (
            select(AnnotationSet)
            .join(Map, Map.id == AnnotationSet.map_id)
            .join(Project, Project.id == Map.project_id)
            .where(
                AnnotationSet.id == set_id,
                AnnotationSet.deleted_at.is_(None),
                Project.organization_id == organization_id,
            )
        )
        if map_id is not None:
            query = query.where(AnnotationSet.map_id == map_id)
        result = await self.db.execute(query)
        annotation_set = result.scalar_one_or_none()
        if annotation_set is None:
            raise not_found("AnnotationSet")
        return annotation_set

    async def create_set(
        self,
        payload: AnnotationSetCreate,
        organization_id: UUID,
        created_by_user_id: UUID | None,
    ) -> AnnotationSet:
        await self._get_map_for_org(payload.map_id, organization_id)
        if payload.schema_id is not None:
            await self._require_schema_for_org(payload.schema_id, organization_id)
        if payload.dataset_id is not None:
            await self._require_dataset_for_org(payload.dataset_id, organization_id)

        data = payload.model_dump()
        data["created_by_user_id"] = created_by_user_id
        annotation_set = AnnotationSet(**data)
        self.db.add(annotation_set)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_annotation_set_conflict map_id=%s", payload.map_id)
            raise conflict("Annotation set violates constraints") from exc
        await self.db.refresh(annotation_set)
        return annotation_set

    async def update_set(
        self,
        set_id: UUID,
        payload: AnnotationSetUpdate,
        organization_id: UUID,
        map_id: UUID | None = None,
    ) -> AnnotationSet:
        annotation_set = await self.get_set(set_id, organization_id, map_id=map_id)
        if payload.schema_id is not None:
            await self._require_schema_for_org(payload.schema_id, organization_id)
        if payload.dataset_id is not None:
            await self._require_dataset_for_org(payload.dataset_id, organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(annotation_set, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set update violates constraints") from exc
        await self.db.refresh(annotation_set)
        return annotation_set

    async def delete_set(
        self, set_id: UUID, organization_id: UUID, map_id: UUID | None = None
    ) -> None:
        annotation_set = await self.get_set(set_id, organization_id, map_id=map_id)
        annotation_set.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
