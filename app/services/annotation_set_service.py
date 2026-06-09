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
    ) -> tuple[list[tuple[MapAnnotationSet, AnnotationSet, str | None]], int]:
        """Returns (mount, joined_set, stac_item_id) tuples so the endpoint can
        flatten the AnnotationSet's identifying fields (name, schema_id,
        job_id, dataset_item_id) plus the dataset_item's stac_item_id into the
        mount response without a second round-trip."""
        await self._get_map_for_org(map_id, organization_id)
        query = (
            select(MapAnnotationSet, AnnotationSet, DatasetItem.stac_item_id)
            .join(AnnotationSet, AnnotationSet.id == MapAnnotationSet.annotation_set_id)
            .outerjoin(DatasetItem, DatasetItem.id == AnnotationSet.dataset_item_id)
            .where(
                MapAnnotationSet.map_id == map_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
            .order_by(MapAnnotationSet.z_index.asc())
        )
        count_query = (
            select(func.count())
            .select_from(MapAnnotationSet)
            .join(AnnotationSet, AnnotationSet.id == MapAnnotationSet.annotation_set_id)
            .where(
                MapAnnotationSet.map_id == map_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        result = await self.db.execute(query)
        rows = [(mount, ann_set, stac_id) for mount, ann_set, stac_id in result.all()]
        total = await self.db.scalar(count_query)
        return rows, int(total or 0)

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
            await self.db.flush()
            from app.services.annotation_set_grouping import ensure_schema_collection_async
            await ensure_schema_collection_async(self.db, annotation_set)
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set violates constraints") from exc
        await self.db.refresh(annotation_set)
        return annotation_set

    async def ensure_annotation_set(
        self,
        *,
        map_id: UUID,
        organization_id: UUID,
        created_by_user_id: UUID,
        schema_id: UUID,
        dataset_id: UUID | None = None,
        name: str | None = None,
    ) -> AnnotationSet:
        """Find — or create — the mutable manual annotation set for a given
        map + schema + user, ensuring it is mounted on the map.

        Human map drawing accumulates into a single per-(map, schema, user)
        set instead of spawning one set per annotation. Only ``manual`` sets
        are matched, so model/import/analysis sets are never reused here. The
        set always carries a non-null ``schema_id``, keeping it eligible for
        single-schema annotation-set collections.
        """
        await self._get_map_for_org(map_id, organization_id)
        await self._require_schema_for_org(schema_id, organization_id)

        existing = await self.db.execute(
            select(AnnotationSet)
            .join(MapAnnotationSet, MapAnnotationSet.annotation_set_id == AnnotationSet.id)
            .where(
                MapAnnotationSet.map_id == map_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.schema_id == schema_id,
                AnnotationSet.source_type == "manual",
                AnnotationSet.created_by_user_id == created_by_user_id,
                AnnotationSet.deleted_at.is_(None),
            )
            .order_by(AnnotationSet.created_at.asc())
            .limit(1)
        )
        annotation_set = existing.scalar_one_or_none()
        if annotation_set is not None:
            return annotation_set

        created = await self.create_set(
            AnnotationSetCreate(
                schema_id=schema_id,
                dataset_id=dataset_id,
                source_type="manual",
                name=name or "Manual annotations",
            ),
            organization_id=organization_id,
            created_by_user_id=created_by_user_id,
        )
        await self.mount_set_on_map(
            map_id,
            AnnotationSetMountRequest(annotation_set_id=created.id),
            organization_id,
        )
        return created

    async def ensure_verified_set(
        self,
        *,
        map_id: UUID,
        organization_id: UUID,
        schema_id: UUID,
        created_by_user_id: UUID,
        dataset_id: UUID | None = None,
        schema_name: str | None = None,
    ) -> tuple[AnnotationSet, bool]:
        """Find — or create — the single human-verified set for a map + schema.

        Verified annotations from every AOI accumulate into ONE durable set per
        (map, schema). AOI grouping is a UI lens driven by per-annotation
        ``properties.aoi_*`` metadata, never a separate set per AOI — AOIs are
        deletable and verified ground-truth must outlive them. Matched only
        among ``manual`` sets flagged ``review_status='verified'``.

        Returns ``(set, created)`` so callers can tell the UI whether a brand
        new set was mounted (and therefore needs to be added as a map layer).
        """
        map_row = await self._get_map_for_org(map_id, organization_id)
        await self._require_schema_for_org(schema_id, organization_id)

        existing = await self.db.execute(
            select(AnnotationSet)
            .join(MapAnnotationSet, MapAnnotationSet.annotation_set_id == AnnotationSet.id)
            .where(
                MapAnnotationSet.map_id == map_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.schema_id == schema_id,
                AnnotationSet.dataset_id == dataset_id,
                AnnotationSet.source_type == "manual",
                AnnotationSet.review_status == "verified",
                AnnotationSet.deleted_at.is_(None),
            )
            .order_by(AnnotationSet.created_at.asc())
            .limit(1)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            return found, False

        # Name reflects both the schema and (when dataset-scoped) the dataset, so
        # the per-dataset verified sets are distinguishable in the layer list.
        dataset_name = None
        if dataset_id is not None:
            dataset_name = await self.db.scalar(
                select(Dataset.name).where(Dataset.id == dataset_id)
            )
        if schema_name and dataset_name:
            name = f"Verified — {schema_name} ({dataset_name})"
        elif schema_name:
            name = f"Verified — {schema_name}"
        else:
            name = "Verified annotations"
        created = await self.create_set(
            AnnotationSetCreate(
                schema_id=schema_id,
                dataset_id=dataset_id,
                source_type="manual",
                name=name,
            ),
            organization_id=organization_id,
            created_by_user_id=created_by_user_id,
        )
        created.review_status = "verified"
        await self.db.commit()
        await self.db.refresh(created)

        await self.mount_set_on_map(
            map_id,
            AnnotationSetMountRequest(annotation_set_id=created.id),
            organization_id,
        )
        await self.link_set_to_project(
            map_row.project_id, created.id, organization_id, linked_by=created_by_user_id
        )
        return created, True

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

    async def set_review_status(
        self, set_id: UUID, review_status: str, organization_id: UUID
    ) -> AnnotationSet:
        annotation_set = await self.get_set(set_id, organization_id)
        annotation_set.review_status = review_status
        await self.db.commit()
        await self.db.refresh(annotation_set)
        return annotation_set

    def mark_corrected_if_model(self, annotation_set: AnnotationSet) -> None:
        """Promote a model-sourced set to 'corrected' the first time a human
        touches one of its annotations. Verified sets are left untouched —
        re-opening a verified set is an explicit decision made through the
        review-status endpoint, not an implicit side effect of an edit.

        Mutates in place; the caller's commit persists it.
        """
        if annotation_set.source_type == "model" and annotation_set.review_status == "raw":
            annotation_set.review_status = "corrected"

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
