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
from app.models.dataset_item import DatasetItem
from app.models.map import Map
from app.models.map_annotation_set import MapAnnotationSet
from app.models.project import Project
from app.models.project_annotation_set import ProjectAnnotationSet
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetMountRequest,
    AnnotationSetMountUpdate,
    AnnotationSetUpdate,
)

logger = logging.getLogger(__name__)


def _set_in_org_clause(organization_id: UUID):
    """Return a WHERE clause fragment scoping AnnotationSet to the given org."""
    return AnnotationSet.organization_id == organization_id


class AnnotationSetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_project_for_org(self, project_id: UUID, organization_id: UUID) -> Project:
        result = await self.db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
            )
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise not_found("Project")
        return project

    async def _get_map_for_org(self, map_id: UUID, organization_id: UUID) -> Map:
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

    async def _require_schema_for_org(self, schema_id: UUID, organization_id: UUID) -> None:
        result = await self.db.execute(
            select(AnnotationSchema.id).where(
                AnnotationSchema.id == schema_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("AnnotationSchema")

    async def _require_dataset_for_org(self, dataset_id: UUID, organization_id: UUID) -> None:
        result = await self.db.execute(
            select(Dataset.id).where(
                Dataset.id == dataset_id,
                Dataset.organization_id == organization_id,
                Dataset.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("Dataset")

    async def _require_dataset_item_for_org(self, dataset_item_id: UUID, organization_id: UUID) -> None:
        result = await self.db.execute(
            select(DatasetItem.id)
            .join(Dataset, Dataset.id == DatasetItem.dataset_id)
            .where(
                DatasetItem.id == dataset_item_id,
                Dataset.organization_id == organization_id,
                Dataset.deleted_at.is_(None),
            )
        )
        if result.scalar_one_or_none() is None:
            raise not_found("DatasetItem")

    async def list_sets(
        self,
        limit: int,
        offset: int,
        organization_id: UUID,
        source_type: str | None = None,
        schema_id: UUID | None = None,
        dataset_id: UUID | None = None,
        model_id: UUID | None = None,
    ) -> tuple[Sequence[AnnotationSet], int]:
        query = select(AnnotationSet).where(
            AnnotationSet.organization_id == organization_id,
            AnnotationSet.deleted_at.is_(None),
        )
        count_query = (
            select(func.count())
            .select_from(AnnotationSet)
            .where(
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )

        if source_type is not None:
            query = query.where(AnnotationSet.source_type == source_type)
            count_query = count_query.where(AnnotationSet.source_type == source_type)
        if schema_id is not None:
            query = query.where(AnnotationSet.schema_id == schema_id)
            count_query = count_query.where(AnnotationSet.schema_id == schema_id)
        if dataset_id is not None:
            query = query.where(AnnotationSet.dataset_id == dataset_id)
            count_query = count_query.where(AnnotationSet.dataset_id == dataset_id)
        if model_id is not None:
            query = query.where(AnnotationSet.model_id == model_id)
            count_query = count_query.where(AnnotationSet.model_id == model_id)

        rows = await self.db.scalars(
            query.order_by(AnnotationSet.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def list_project_sets(
        self,
        project_id: UUID,
        organization_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[AnnotationSet], int]:
        await self._get_project_for_org(project_id, organization_id)
        query = (
            select(AnnotationSet)
            .join(ProjectAnnotationSet, ProjectAnnotationSet.annotation_set_id == AnnotationSet.id)
            .where(
                ProjectAnnotationSet.project_id == project_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        count_query = (
            select(func.count())
            .select_from(ProjectAnnotationSet)
            .join(AnnotationSet, AnnotationSet.id == ProjectAnnotationSet.annotation_set_id)
            .where(
                ProjectAnnotationSet.project_id == project_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        rows = await self.db.scalars(
            query.order_by(AnnotationSet.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def list_map_mounts(
        self, map_id: UUID, organization_id: UUID
    ) -> tuple[Sequence[MapAnnotationSet], int]:
        await self._get_map_for_org(map_id, organization_id)
        query = select(MapAnnotationSet).where(MapAnnotationSet.map_id == map_id)
        count_query = (
            select(func.count())
            .select_from(MapAnnotationSet)
            .where(MapAnnotationSet.map_id == map_id)
        )
        rows = await self.db.scalars(query.order_by(MapAnnotationSet.z_index.asc()))
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_set(self, set_id: UUID, organization_id: UUID) -> AnnotationSet:
        result = await self.db.execute(
            select(AnnotationSet).where(
                AnnotationSet.id == set_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
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
        if payload.schema_id is not None:
            await self._require_schema_for_org(payload.schema_id, organization_id)
        if payload.dataset_id is not None:
            await self._require_dataset_for_org(payload.dataset_id, organization_id)
        if payload.dataset_item_id is not None:
            await self._require_dataset_item_for_org(payload.dataset_item_id, organization_id)

        data = payload.model_dump()
        data["organization_id"] = organization_id
        data["created_by_user_id"] = created_by_user_id
        annotation_set = AnnotationSet(**data)
        self.db.add(annotation_set)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set violates constraints") from exc
        await self.db.refresh(annotation_set)
        return annotation_set

    async def update_set(
        self,
        set_id: UUID,
        payload: AnnotationSetUpdate,
        organization_id: UUID,
    ) -> AnnotationSet:
        annotation_set = await self.get_set(set_id, organization_id)
        if payload.schema_id is not None:
            await self._require_schema_for_org(payload.schema_id, organization_id)
        if payload.dataset_id is not None:
            await self._require_dataset_for_org(payload.dataset_id, organization_id)
        if payload.dataset_item_id is not None:
            await self._require_dataset_item_for_org(payload.dataset_item_id, organization_id)

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

    async def delete_set(self, set_id: UUID, organization_id: UUID) -> None:
        annotation_set = await self.get_set(set_id, organization_id)
        annotation_set.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()

    async def link_set_to_project(
        self,
        project_id: UUID,
        annotation_set_id: UUID,
        organization_id: UUID,
        linked_by: UUID | None,
    ) -> ProjectAnnotationSet:
        await self._get_project_for_org(project_id, organization_id)
        await self.get_set(annotation_set_id, organization_id)

        existing = await self.db.get(
            ProjectAnnotationSet, {"project_id": project_id, "annotation_set_id": annotation_set_id}
        )
        if existing is not None:
            return existing

        link = ProjectAnnotationSet(
            project_id=project_id,
            annotation_set_id=annotation_set_id,
            linked_by=linked_by,
        )
        self.db.add(link)
        await self.db.commit()
        await self.db.refresh(link)
        return link

    async def unlink_set_from_project(
        self,
        project_id: UUID,
        annotation_set_id: UUID,
        organization_id: UUID,
    ) -> None:
        await self._get_project_for_org(project_id, organization_id)
        await self.get_set(annotation_set_id, organization_id)

        link = await self.db.get(
            ProjectAnnotationSet, {"project_id": project_id, "annotation_set_id": annotation_set_id}
        )
        if link is None:
            raise not_found("ProjectAnnotationSet")
        await self.db.delete(link)
        await self.db.commit()

    async def mount_set_on_map(
        self,
        map_id: UUID,
        payload: AnnotationSetMountRequest,
        organization_id: UUID,
    ) -> MapAnnotationSet:
        await self._get_map_for_org(map_id, organization_id)
        await self.get_set(payload.annotation_set_id, organization_id)

        existing = await self.db.get(
            MapAnnotationSet,
            {"map_id": map_id, "annotation_set_id": payload.annotation_set_id},
        )
        if existing is not None:
            return existing

        mount = MapAnnotationSet(
            map_id=map_id,
            annotation_set_id=payload.annotation_set_id,
            visible=payload.visible,
            opacity=payload.opacity,
            z_index=payload.z_index,
            style_id=payload.style_id,
            style_override=payload.style_override,
        )
        self.db.add(mount)
        await self.db.commit()
        await self.db.refresh(mount)
        return mount

    async def update_map_mount(
        self,
        map_id: UUID,
        annotation_set_id: UUID,
        payload: AnnotationSetMountUpdate,
        organization_id: UUID,
    ) -> MapAnnotationSet:
        await self._get_map_for_org(map_id, organization_id)
        await self.get_set(annotation_set_id, organization_id)

        mount = await self.db.get(
            MapAnnotationSet, {"map_id": map_id, "annotation_set_id": annotation_set_id}
        )
        if mount is None:
            raise not_found("MapAnnotationSet")

        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(mount, key, value)

        await self.db.commit()
        await self.db.refresh(mount)
        return mount

    async def unmount_set_from_map(
        self,
        map_id: UUID,
        annotation_set_id: UUID,
        organization_id: UUID,
    ) -> None:
        await self._get_map_for_org(map_id, organization_id)
        await self.get_set(annotation_set_id, organization_id)

        mount = await self.db.get(
            MapAnnotationSet, {"map_id": map_id, "annotation_set_id": annotation_set_id}
        )
        if mount is None:
            raise not_found("MapAnnotationSet")
        await self.db.delete(mount)
        await self.db.commit()
