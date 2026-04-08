import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from shapely.geometry import shape as shape_geom
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import bad_request, conflict, not_found
from app.core.geometry import parse_geometry
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.schemas.annotation import AnnotationCreate, AnnotationUpdate

logger = logging.getLogger(__name__)


def _normalize_geom_type(value: str) -> str:
    if value.startswith("Multi"):
        return value.replace("Multi", "", 1)
    return value


class AnnotationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_set_for_org(
        self, set_id: UUID, organization_id: UUID
    ) -> AnnotationSet:
        result = await self.db.execute(
            select(AnnotationSet)
            .where(
                AnnotationSet.id == set_id,
                AnnotationSet.deleted_at.is_(None),
                AnnotationSet.organization_id == organization_id,
            )
        )
        annotation_set = result.scalar_one_or_none()
        if annotation_set is None:
            raise not_found("AnnotationSet")
        return annotation_set

    async def _get_class_and_schema(
        self, class_id: UUID, organization_id: UUID
    ) -> tuple[AnnotationClass, AnnotationSchema]:
        result = await self.db.execute(
            select(AnnotationClass, AnnotationSchema)
            .join(AnnotationSchema, AnnotationSchema.id == AnnotationClass.schema_id)
            .where(
                AnnotationClass.id == class_id,
                AnnotationSchema.organization_id == organization_id,
                AnnotationSchema.deleted_at.is_(None),
            )
        )
        row = result.first()
        if row is None:
            raise not_found("AnnotationClass")
        return row[0], row[1]

    def _validate_geometry(
        self, geometry: dict, allowed_types: list[str] | None
    ) -> None:
        if not allowed_types:
            return
        geom_type = _normalize_geom_type(shape_geom(geometry).geom_type)
        if geom_type not in allowed_types:
            raise bad_request(
                f"Geometry type '{geom_type}' is not allowed for this schema"
            )

    async def list_annotations(
        self,
        set_id: UUID,
        limit: int,
        offset: int,
        organization_id: UUID,
    ) -> tuple[Sequence[Annotation], int]:
        await self._get_set_for_org(set_id, organization_id)
        query = select(Annotation).where(
            Annotation.annotation_set_id == set_id,
            Annotation.deleted_at.is_(None),
        )
        count_query = (
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.annotation_set_id == set_id,
                Annotation.deleted_at.is_(None),
            )
        )
        rows = await self.db.scalars(
            query.order_by(Annotation.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_annotation(
        self, annotation_id: UUID, organization_id: UUID, set_id: UUID | None = None
    ) -> Annotation:
        query = (
            select(Annotation)
            .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
            .where(
                Annotation.id == annotation_id,
                Annotation.deleted_at.is_(None),
                AnnotationSet.organization_id == organization_id,
            )
        )
        if set_id is not None:
            query = query.where(Annotation.annotation_set_id == set_id)
        result = await self.db.execute(query)
        annotation = result.scalar_one_or_none()
        if annotation is None:
            raise not_found("Annotation")
        return annotation

    async def create_annotation(
        self,
        set_id: UUID,
        payload: AnnotationCreate,
        organization_id: UUID,
    ) -> Annotation:
        annotation_set = await self._get_set_for_org(set_id, organization_id)
        cls, schema = await self._get_class_and_schema(payload.class_id, organization_id)

        if annotation_set.schema_id and annotation_set.schema_id != cls.schema_id:
            raise bad_request("Annotation class does not match the set schema")

        self._validate_geometry(payload.geometry, schema.geometry_types)

        data = payload.model_dump()
        data["annotation_set_id"] = set_id
        data["geometry"] = parse_geometry(payload.geometry)
        annotation = Annotation(**data)
        self.db.add(annotation)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation violates constraints") from exc
        await self.db.refresh(annotation)
        return annotation

    async def update_annotation(
        self,
        annotation_id: UUID,
        payload: AnnotationUpdate,
        organization_id: UUID,
        set_id: UUID | None = None,
    ) -> Annotation:
        annotation = await self.get_annotation(annotation_id, organization_id, set_id=set_id)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise bad_request("No fields to update")

        result = await self.db.execute(
            select(AnnotationSet.schema_id).where(AnnotationSet.id == annotation.annotation_set_id)
        )
        set_schema_id = result.scalar_one_or_none()

        if "class_id" in data:
            cls, schema = await self._get_class_and_schema(data["class_id"], organization_id)
            if set_schema_id and set_schema_id != cls.schema_id:
                raise bad_request("Annotation class does not match the set schema")
            if "geometry" in data:
                self._validate_geometry(data["geometry"], schema.geometry_types)
        elif "geometry" in data:
            if set_schema_id:
                result = await self.db.execute(
                    select(AnnotationSchema.geometry_types)
                    .where(AnnotationSchema.id == set_schema_id)
                )
                allowed = result.scalar_one_or_none()
                self._validate_geometry(data["geometry"], allowed)

        if "geometry" in data:
            data["geometry"] = parse_geometry(data["geometry"])

        for key, value in data.items():
            setattr(annotation, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Annotation update violates constraints") from exc
        await self.db.refresh(annotation)
        return annotation

    async def delete_annotation(
        self, annotation_id: UUID, organization_id: UUID, set_id: UUID | None = None
    ) -> None:
        annotation = await self.get_annotation(annotation_id, organization_id, set_id=set_id)
        annotation.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
