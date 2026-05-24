import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import bad_request, conflict, not_found
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.annotation_set_collection_item import AnnotationSetCollectionItem
from app.schemas.annotation_set_collection import (
    AnnotationSetCollectionCreate,
    AnnotationSetCollectionUpdate,
)

logger = logging.getLogger(__name__)


class AnnotationSetCollectionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

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

    async def _get_set_for_org(self, annotation_set_id: UUID, organization_id: UUID) -> AnnotationSet:
        result = await self.db.execute(
            select(AnnotationSet).where(
                AnnotationSet.id == annotation_set_id,
                AnnotationSet.organization_id == organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        annotation_set = result.scalar_one_or_none()
        if annotation_set is None:
            raise not_found("AnnotationSet")
        return annotation_set

    async def list_collections(
        self,
        *,
        organization_id: UUID,
        limit: int,
        offset: int,
        schema_id: UUID | None = None,
    ) -> tuple[Sequence[AnnotationSetCollection], int]:
        query = select(AnnotationSetCollection).where(
            AnnotationSetCollection.organization_id == organization_id,
            AnnotationSetCollection.deleted_at.is_(None),
        )
        count_query = select(func.count()).select_from(AnnotationSetCollection).where(
            AnnotationSetCollection.organization_id == organization_id,
            AnnotationSetCollection.deleted_at.is_(None),
        )
        if schema_id is not None:
            query = query.where(AnnotationSetCollection.schema_id == schema_id)
            count_query = count_query.where(AnnotationSetCollection.schema_id == schema_id)

        rows = await self.db.scalars(
            query.order_by(AnnotationSetCollection.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_collection(self, collection_id: UUID, *, organization_id: UUID) -> AnnotationSetCollection:
        result = await self.db.execute(
            select(AnnotationSetCollection).where(
                AnnotationSetCollection.id == collection_id,
                AnnotationSetCollection.organization_id == organization_id,
                AnnotationSetCollection.deleted_at.is_(None),
            )
        )
        collection = result.scalar_one_or_none()
        if collection is None:
            raise not_found("Annotation set collection")
        return collection

    async def create_collection(
        self,
        payload: AnnotationSetCollectionCreate,
        *,
        organization_id: UUID,
        created_by: UUID | None,
    ) -> AnnotationSetCollection:
        await self._require_schema_for_org(payload.schema_id, organization_id)
        collection = AnnotationSetCollection(
            organization_id=organization_id,
            schema_id=payload.schema_id,
            name=payload.name,
            description=payload.description,
            created_by=created_by,
        )
        self.db.add(collection)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set collection violates constraints") from exc
        await self.db.refresh(collection)
        return collection

    async def update_collection(
        self,
        collection_id: UUID,
        payload: AnnotationSetCollectionUpdate,
        *,
        organization_id: UUID,
    ) -> AnnotationSetCollection:
        collection = await self.get_collection(collection_id, organization_id=organization_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(collection, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set collection update violates constraints") from exc
        await self.db.refresh(collection)
        return collection

    async def delete_collection(self, collection_id: UUID, *, organization_id: UUID) -> None:
        collection = await self.get_collection(collection_id, organization_id=organization_id)
        collection.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()

    async def list_collection_sets(
        self,
        collection_id: UUID,
        *,
        organization_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[AnnotationSet], int]:
        await self.get_collection(collection_id, organization_id=organization_id)
        query = (
            select(AnnotationSet)
            .join(
                AnnotationSetCollectionItem,
                AnnotationSetCollectionItem.annotation_set_id == AnnotationSet.id,
            )
            .where(
                AnnotationSetCollectionItem.collection_id == collection_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        count_query = (
            select(func.count())
            .select_from(AnnotationSetCollectionItem)
            .join(AnnotationSet, AnnotationSet.id == AnnotationSetCollectionItem.annotation_set_id)
            .where(
                AnnotationSetCollectionItem.collection_id == collection_id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
        rows = await self.db.scalars(
            query.order_by(AnnotationSet.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def add_set_to_collection(
        self,
        collection_id: UUID,
        annotation_set_id: UUID,
        *,
        organization_id: UUID,
        linked_by: UUID | None,
    ) -> AnnotationSetCollectionItem:
        collection = await self.get_collection(collection_id, organization_id=organization_id)
        annotation_set = await self._get_set_for_org(annotation_set_id, organization_id)
        if annotation_set.schema_id is None:
            raise bad_request("Annotation set must have a schema to be added to a collection")
        if annotation_set.schema_id != collection.schema_id:
            raise bad_request("Annotation set schema must match the collection schema")

        link = AnnotationSetCollectionItem(
            collection_id=collection_id,
            annotation_set_id=annotation_set_id,
            linked_by=linked_by,
        )
        self.db.add(link)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation set is already linked to this collection") from exc
        await self.db.refresh(link)
        return link

    async def remove_set_from_collection(
        self,
        collection_id: UUID,
        annotation_set_id: UUID,
        *,
        organization_id: UUID,
    ) -> None:
        await self.get_collection(collection_id, organization_id=organization_id)
        result = await self.db.execute(
            select(AnnotationSetCollectionItem).where(
                AnnotationSetCollectionItem.collection_id == collection_id,
                AnnotationSetCollectionItem.annotation_set_id == annotation_set_id,
            )
        )
        link = result.scalar_one_or_none()
        if link is None:
            raise not_found("Annotation set collection item")
        await self.db.delete(link)
        await self.db.commit()
