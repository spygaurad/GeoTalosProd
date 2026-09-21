"""YOLO training-data export: validation + manifest building only.

Pure DB-read logic plus geometry math — no TiTiler calls, no S3 writes, no
torch/ultralytics import. Actually materializing images + YOLO label files is
a later step (the Celery task that calls this to validate, then does the I/O
and hands the result to yolo-service).

Reaches into ``app/`` only for the models it references by FK
(``AnnotationSetCollection``, ``AnnotationSet``, ``Annotation``,
``AnnotationClass``, ``DatasetItem``) — same convention as
``geoops/service.py``.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from shapely.geometry import shape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import not_found
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.dataset_item import DatasetItem

from geoops.export_common import partition_member_sets
from geoops.yolo_schemas import ClassCount, YoloExportPreviewResponse


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def geometry_to_yolo_bbox(geometry: dict[str, Any], item_bbox: list[float]) -> tuple[float, float, float, float]:
    """GeoJSON geometry (EPSG:4326) -> normalized YOLO (cx, cy, w, h) within item_bbox.

    ``item_bbox`` is the source image's [minx, miny, maxx, maxy] in the same
    CRS. Image row 0 is the image's *top* (maxy), so the y-axis is flipped
    relative to lat, which increases upward.
    """
    minx, miny, maxx, maxy = shape(geometry).bounds
    ix0, iy0, ix1, iy1 = item_bbox
    span_x, span_y = ix1 - ix0, iy1 - iy0
    if span_x <= 0 or span_y <= 0:
        raise ValueError("item_bbox has zero or negative span")

    px0, px1 = (minx - ix0) / span_x, (maxx - ix0) / span_x
    py0, py1 = (iy1 - maxy) / span_y, (iy1 - miny) / span_y
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    w, h = px1 - px0, py1 - py0
    return _clamp01(cx), _clamp01(cy), _clamp01(w), _clamp01(h)


def geometry_to_yolo_polygon(geometry: dict[str, Any], item_bbox: list[float]) -> list[float]:
    """GeoJSON geometry -> flat normalized YOLO-segment polygon [x1, y1, x2, y2, ...].

    MultiPolygon is reduced to its largest part (YOLO-seg labels are one
    polygon per label line; splitting a multi-part annotation into separate
    lines under the same class is a caller-level decision, not this
    function's).
    """
    geom = shape(geometry)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    if geom.geom_type != "Polygon":
        raise ValueError(f"Unsupported geometry type for segmentation export: {geom.geom_type}")

    ix0, iy0, ix1, iy1 = item_bbox
    span_x, span_y = ix1 - ix0, iy1 - iy0
    if span_x <= 0 or span_y <= 0:
        raise ValueError("item_bbox has zero or negative span")

    coords: list[float] = []
    for x, y in geom.exterior.coords[:-1]:  # drop the closing duplicate point
        coords.append(_clamp01((x - ix0) / span_x))
        coords.append(_clamp01((iy1 - y) / span_y))
    return coords


class YoloExportService:
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

    async def build_preview(self, org_id: UUID, collection_id: UUID) -> YoloExportPreviewResponse:
        collection = await self._get_collection(org_id, collection_id)
        included_set_ids, skipped = await partition_member_sets(self.session, collection)

        if not included_set_ids:
            return YoloExportPreviewResponse(
                annotation_set_collection_id=collection_id,
                schema_id=collection.schema_id,
                geometry_types=[],
                verified_sets_included=0,
                sets_skipped=skipped,
                dataset_item_count=0,
                total_annotations=0,
                annotations_skipped_no_item=0,
                annotations_skipped_inactive_item=0,
                classes=[],
                ready=False,
            )

        # dataset_item resolution: prefer the annotation's own stamp, fall
        # back to the set's item scope for older rows created before
        # annotations.dataset_item_id existed in an item-scoped set.
        item_id_expr = func.coalesce(Annotation.dataset_item_id, AnnotationSet.dataset_item_id)

        rows = (
            await self.session.execute(
                select(
                    item_id_expr.label("item_id"),
                    Annotation.class_id,
                    AnnotationClass.name.label("class_name"),
                    DatasetItem.is_active,
                )
                .select_from(Annotation)
                .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
                .join(AnnotationClass, AnnotationClass.id == Annotation.class_id)
                .outerjoin(DatasetItem, DatasetItem.id == item_id_expr)
                .where(
                    Annotation.annotation_set_id.in_(included_set_ids),
                    Annotation.deleted_at.is_(None),
                )
            )
        ).all()

        eligible_items: set[UUID] = set()
        class_counts: dict[UUID, dict[str, Any]] = {}
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
            bucket = class_counts.setdefault(
                row.class_id, {"name": row.class_name, "annotation_count": 0, "items": set()}
            )
            bucket["annotation_count"] += 1
            bucket["items"].add(row.item_id)

        classes = [
            ClassCount(
                class_id=class_id,
                name=bucket["name"],
                annotation_count=bucket["annotation_count"],
                dataset_item_count=len(bucket["items"]),
            )
            for class_id, bucket in sorted(class_counts.items(), key=lambda kv: kv[1]["name"])
        ]

        schema = await self.session.get(AnnotationSchema, collection.schema_id)

        return YoloExportPreviewResponse(
            annotation_set_collection_id=collection_id,
            schema_id=collection.schema_id,
            geometry_types=list(schema.geometry_types) if schema else [],
            verified_sets_included=len(included_set_ids),
            sets_skipped=skipped,
            dataset_item_count=len(eligible_items),
            total_annotations=total_eligible,
            annotations_skipped_no_item=skipped_no_item,
            annotations_skipped_inactive_item=skipped_inactive_item,
            classes=classes,
            ready=total_eligible > 0,
        )
