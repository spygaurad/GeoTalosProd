"""DINOv2-tier training-data export: validation + manifest building only.

Pure DB-read logic — no TiTiler calls, no S3 writes, no torch/transformers
import. Scoped to ONE class per call (unlike geoops.yolo_service, which is
collection-wide/multi-class) because this tier trains one head per class.

Reuses the pure geometry_to_yolo_bbox/geometry_to_yolo_polygon conversion
math from geoops.yolo_service — that math is generic normalized-coordinate
conversion, not YOLO-label-format specific; only the serialization (YOLO
.txt lines vs this tier's JSON manifest) differs.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import not_found
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.dataset_item import DatasetItem

from geoops.dino_schemas import DinoExportPreviewResponse
from geoops.export_common import partition_member_sets


class DinoExportService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_collection(self, org_id: UUID, collection_id: UUID) -> AnnotationSetCollection:
        collection = await self.session.scalar(
            select(AnnotationSetCollection).where(
                AnnotationSetCollection.id == collection_id,
                AnnotationSetCollection.organization_id == org_id,
                AnnotationSetCollection.deleted_at.is_(None),
            )
        )
        if collection is None:
            raise not_found("AnnotationSetCollection")
        return collection

    async def _get_class(self, org_id: UUID, class_id: UUID, schema_id: UUID) -> AnnotationClass:
        cls = await self.session.scalar(
            select(AnnotationClass)
            .join(AnnotationSchema, AnnotationSchema.id == AnnotationClass.schema_id)
            .where(
                AnnotationClass.id == class_id,
                AnnotationClass.schema_id == schema_id,
                AnnotationSchema.organization_id == org_id,
            )
        )
        if cls is None:
            raise not_found("AnnotationClass (must belong to the collection's schema)")
        return cls

    async def build_preview(
        self, org_id: UUID, collection_id: UUID, class_id: UUID
    ) -> DinoExportPreviewResponse:
        collection = await self._get_collection(org_id, collection_id)
        cls = await self._get_class(org_id, class_id, collection.schema_id)
        included_set_ids, skipped = await partition_member_sets(self.session, collection)

        schema = await self.session.get(AnnotationSchema, collection.schema_id)

        if not included_set_ids:
            return DinoExportPreviewResponse(
                annotation_set_collection_id=collection_id,
                class_id=class_id,
                class_name=cls.name,
                schema_id=collection.schema_id,
                geometry_types=list(schema.geometry_types) if schema else [],
                verified_sets_included=0,
                sets_skipped=skipped,
                dataset_item_count=0,
                total_annotations=0,
                annotations_skipped_no_item=0,
                annotations_skipped_inactive_item=0,
                ready=False,
            )

        item_id_expr = func.coalesce(Annotation.dataset_item_id, AnnotationSet.dataset_item_id)
        rows = (
            await self.session.execute(
                select(item_id_expr.label("item_id"), DatasetItem.is_active)
                .select_from(Annotation)
                .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
                .outerjoin(DatasetItem, DatasetItem.id == item_id_expr)
                .where(
                    Annotation.annotation_set_id.in_(included_set_ids),
                    Annotation.class_id == class_id,
                    Annotation.deleted_at.is_(None),
                )
            )
        ).all()

        eligible_items: set[UUID] = set()
        total_eligible = 0
        skipped_no_item = 0
        skipped_inactive_item = 0
        for row in rows:
            if row.item_id is None:
                skipped_no_item += 1
                continue
            if not row.is_active:
                skipped_inactive_item += 1
                continue
            total_eligible += 1
            eligible_items.add(row.item_id)

        return DinoExportPreviewResponse(
            annotation_set_collection_id=collection_id,
            class_id=class_id,
            class_name=cls.name,
            schema_id=collection.schema_id,
            geometry_types=list(schema.geometry_types) if schema else [],
            verified_sets_included=len(included_set_ids),
            sets_skipped=skipped,
            dataset_item_count=len(eligible_items),
            total_annotations=total_eligible,
            annotations_skipped_no_item=skipped_no_item,
            annotations_skipped_inactive_item=skipped_inactive_item,
            ready=total_eligible > 0,
        )
