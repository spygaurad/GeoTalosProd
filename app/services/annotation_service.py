import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.core.geometry import parse_geometry
from app.models.annotation import Annotation
from app.schemas.annotation import AnnotationCreate, AnnotationUpdate

logger = logging.getLogger(__name__)


class AnnotationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_annotations(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
        dataset_item_id: UUID | None = None,
        stac_item_id: str | None = None,
        track_id: UUID | None = None,
        label: str | None = None,
        status: str | None = None,
    ) -> tuple[Sequence[Annotation], int]:
        query = select(Annotation)
        count_query = select(func.count()).select_from(Annotation)

        if organization_id is not None:
            query = query.where(Annotation.organization_id == organization_id)
            count_query = count_query.where(Annotation.organization_id == organization_id)
        if dataset_item_id is not None:
            query = query.where(Annotation.dataset_item_id == dataset_item_id)
            count_query = count_query.where(Annotation.dataset_item_id == dataset_item_id)
        if stac_item_id is not None:
            query = query.where(Annotation.stac_item_id == stac_item_id)
            count_query = count_query.where(Annotation.stac_item_id == stac_item_id)
        if track_id is not None:
            query = query.where(Annotation.track_id == track_id)
            count_query = count_query.where(Annotation.track_id == track_id)
        if label is not None:
            query = query.where(Annotation.label == label)
            count_query = count_query.where(Annotation.label == label)
        if status is not None:
            query = query.where(Annotation.status == status)
            count_query = count_query.where(Annotation.status == status)

        rows = await self.db.scalars(
            query.order_by(Annotation.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_annotations organization_id=%s dataset_item_id=%s stac_item_id=%s track_id=%s label=%s status=%s limit=%s offset=%s total=%s",
            organization_id,
            dataset_item_id,
            stac_item_id,
            track_id,
            label,
            status,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_annotation(
        self, annotation_id: UUID, organization_id: UUID | None = None
    ) -> Annotation:
        if organization_id is None:
            annotation = await self.db.get(Annotation, annotation_id)
        else:
            result = await self.db.execute(
                select(Annotation).where(
                    Annotation.id == annotation_id, Annotation.organization_id == organization_id
                )
            )
            annotation = result.scalar_one_or_none()
        if annotation is None:
            logger.warning("get_annotation_not_found annotation_id=%s", annotation_id)
            raise not_found("Annotation")
        return annotation

    async def create_annotation(self, payload: AnnotationCreate) -> Annotation:
        data = payload.model_dump()
        if data.get("geometry") is not None:
            data["geometry"] = parse_geometry(data["geometry"])
        annotation = Annotation(**data)
        self.db.add(annotation)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_annotation_conflict organization_id=%s", payload.organization_id)
            raise conflict("Annotation creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(annotation)
        logger.info("create_annotation_success annotation_id=%s", annotation.id)
        return annotation

    async def update_annotation(
        self, annotation_id: UUID, payload: AnnotationUpdate, organization_id: UUID | None = None
    ) -> Annotation:
        annotation = await self.get_annotation(annotation_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)

        if "geometry" in data:
            data["geometry"] = parse_geometry(data["geometry"])
        if data.get("tags") is None and "tags" in data:
            data["tags"] = []

        for key, value in data.items():
            setattr(annotation, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_annotation_conflict annotation_id=%s", annotation_id)
            raise conflict("Annotation update violates uniqueness or FK constraints") from exc
        await self.db.refresh(annotation)
        logger.info("update_annotation_success annotation_id=%s", annotation.id)
        return annotation

    async def delete_annotation(self, annotation_id: UUID, organization_id: UUID | None = None) -> None:
        annotation = await self.get_annotation(annotation_id, organization_id=organization_id)
        await self.db.delete(annotation)
        await self.db.commit()
        logger.info("delete_annotation_success annotation_id=%s", annotation_id)
