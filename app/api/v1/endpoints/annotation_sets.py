import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.enums import JobStatus, JobType
from app.core.geometry import serialize_geometry
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetExportRequest,
    AnnotationSetExportResponse,
    AnnotationSetListResponse,
    AnnotationSetLinkRequest,
    AnnotationSetMountListResponse,
    AnnotationSetMountRead,
    AnnotationSetMountRequest,
    AnnotationSetMountUpdate,
    AnnotationSetProjectLinkRead,
    AnnotationSetRead,
    AnnotationSetUpdate,
    RasterMaskConfigRead,
    RasterMaskConfigUpdate,
    RasterMaskValuesPreviewRead,
)
from app.services import storage_service
from app.services.annotation_service import AnnotationService
from app.services.annotation_set_service import AnnotationSetService
from app.workers.ingestion.rasterio_utils import extract_unique_values

logger = logging.getLogger(__name__)

# Limit concurrent rasterio S3 reads to prevent thread pool exhaustion.
_raster_preview_semaphore = asyncio.Semaphore(2)

# 50 MB cap on inline GeoJSON imports.
MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024
EXPORT_PAGE_SIZE = 10_000


# ── Router definitions ────────────────────────────────────────────────────────

router = APIRouter(prefix="/annotation-sets", tags=["annotation-sets"])
set_router = APIRouter(prefix="/annotation-sets/{set_id}", tags=["annotation-sets"])
project_router = APIRouter(prefix="/projects/{project_id}/annotation-sets", tags=["annotation-sets"])
map_router = APIRouter(prefix="/maps/{map_id}/annotation-sets", tags=["annotation-sets"])


# ── Request schemas ───────────────────────────────────────────────────────────

class GeoJSONImportRequest(BaseModel):
    """Inline GeoJSON FeatureCollection import payload."""
    geojson: dict[str, Any] = Field(..., description="A GeoJSON FeatureCollection")
    filename: str | None = Field(default=None, max_length=255)
    default_class_id: UUID | None = None
    class_property: str = Field(default="class_id", max_length=64)
    confidence_property: str | None = Field(default=None, max_length=64)


# ── Private helpers ───────────────────────────────────────────────────────────

def _gdal_env_for_api() -> dict:
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
        "GDAL_HTTP_TIMEOUT": "30",
        "CPL_CURL_GZIP": "YES",
    }


def _hex_to_rgba(value: str) -> list[int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 6:
        raw = f"{raw}ff"
    if len(raw) != 8:
        raise ValueError(f"Invalid hex color: {value!r}")
    try:
        return [int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16)]
    except ValueError:
        raise ValueError(f"Invalid hex color digits: {value!r}")


def _extract_class_fill_rgba(annotation_class: AnnotationClass) -> list[int]:
    style = annotation_class.style
    if style is None:
        return [255, 255, 255, 255]
    definition = style.definition or {}
    fill = definition.get("fillColor") or definition.get("fill") or definition.get("color")
    if isinstance(fill, str):
        try:
            return _hex_to_rgba(fill)
        except ValueError:
            logger.warning("class_fill_invalid_hex class_id=%s value=%r", annotation_class.id, fill)
    return [255, 255, 255, 255]


def _coerce_value_map(raw_map: dict[str, UUID]) -> dict[str, UUID]:
    coerced: dict[str, UUID] = {}
    for raw_key, cls_id in raw_map.items():
        key = str(raw_key).strip()
        try:
            float_val = float(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid raster class value key: {raw_key!r}") from exc
        normalized = str(int(float_val)) if float_val == int(float_val) else str(float_val)
        coerced[normalized] = cls_id
    return coerced


async def _get_dataset_item_for_org(db: AsyncSession, dataset_item_ref: str | UUID, org_id: UUID) -> DatasetItem:
    dataset_item_text = str(dataset_item_ref).strip()
    filters = [DatasetItem.organization_id == org_id]
    if len(dataset_item_text) == 36 and dataset_item_text.count("-") == 4:
        try:
            filters.append(DatasetItem.id == UUID(dataset_item_text))
        except ValueError:
            filters.append(DatasetItem.stac_item_id == dataset_item_text)
    else:
        filters.append(DatasetItem.stac_item_id == dataset_item_text)
    result = await db.execute(select(DatasetItem).where(*filters))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found")
    return item


async def _build_colormap(
    db: AsyncSession,
    *,
    schema_id: UUID,
    value_class_map: dict[str, UUID],
    nodata_value: float | None,
) -> dict[str, list[int]]:
    class_ids = set(value_class_map.values())
    rows = await db.execute(
        select(AnnotationClass)
        .options(selectinload(AnnotationClass.style))
        .where(AnnotationClass.schema_id == schema_id, AnnotationClass.id.in_(class_ids))
    )
    classes = {row.id: row for row in rows.scalars().all()}
    if len(classes) != len(class_ids):
        raise HTTPException(status_code=400, detail="One or more class IDs do not belong to the annotation set schema")
    colormap: dict[str, list[int]] = {}
    for value, cls_id in value_class_map.items():
        colormap[value] = _extract_class_fill_rgba(classes[cls_id])
    if nodata_value is not None:
        fv = float(nodata_value)
        nodata_key = str(int(fv)) if fv == int(fv) else str(fv)
        colormap[nodata_key] = [0, 0, 0, 0]
    return colormap


# ── Org-level CRUD (/annotation-sets) ────────────────────────────────────────

@router.get("", response_model=AnnotationSetListResponse)
async def list_annotation_sets(
    source_type: str | None = Query(default=None),
    schema_id: UUID | None = Query(default=None),
    dataset_id: UUID | None = Query(default=None),
    model_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_sets(
        limit=limit, offset=offset, organization_id=org_id,
        source_type=source_type, schema_id=schema_id, dataset_id=dataset_id, model_id=model_id,
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{set_id}", response_model=AnnotationSetRead)
async def get_annotation_set(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    return await AnnotationSetService(db).get_set(set_id, organization_id=org_id)


@router.post("", response_model=AnnotationSetRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_set(
    payload: AnnotationSetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.create_set(payload, organization_id=org_id, created_by_user_id=current_user.id)
    await log_audit_event(
        action="annotation_sets.create", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(annotation_set.id), session=db,
    )
    return annotation_set


@router.patch("/{set_id}", response_model=AnnotationSetRead)
async def update_annotation_set(
    set_id: UUID,
    payload: AnnotationSetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.update_set(set_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotation_sets.update", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(set_id), session=db,
    )
    return annotation_set


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_set(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await AnnotationSetService(db).delete_set(set_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_sets.delete", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(set_id), session=db,
    )


# ── Set-scoped operations (/annotation-sets/{set_id}/...) ────────────────────

@set_router.get("/bounds")
async def get_annotation_set_bounds(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return the WGS-84 bounding box of all annotations in this set, or null if empty."""
    await AnnotationSetService(db).get_set(set_id, organization_id=org_id)
    row = (await db.execute(
        text("""
            SELECT ST_XMin(ST_Extent(geometry)), ST_YMin(ST_Extent(geometry)),
                   ST_XMax(ST_Extent(geometry)), ST_YMax(ST_Extent(geometry))
            FROM annotations
            WHERE annotation_set_id = :sid AND deleted_at IS NULL
        """),
        {"sid": str(set_id)},
    )).fetchone()
    if row is None or row[0] is None:
        return {"bounds": None}
    return {"bounds": {"west": float(row[0]), "south": float(row[1]), "east": float(row[2]), "north": float(row[3])}}


@set_router.get("/features")
async def get_annotation_set_features(
    set_id: UUID,
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return annotations as a GeoJSON FeatureCollection."""
    ann_service = AnnotationService(db)
    items, total = await ann_service.list_annotations(set_id=set_id, limit=limit, offset=offset, organization_id=org_id)
    features = []
    for ann in items:
        geom = serialize_geometry(ann.geometry)
        if geom is None:
            continue
        features.append({
            "type": "Feature",
            "id": str(ann.id),
            "geometry": geom,
            "properties": {"class_id": str(ann.class_id), "confidence": ann.confidence, **(ann.properties or {})},
        })
    return {"type": "FeatureCollection", "features": features, "total": total}


@set_router.post("/export", response_model=AnnotationSetExportResponse)
async def export_annotation_set(
    set_id: UUID,
    payload: AnnotationSetExportRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    annotation_set = await AnnotationSetService(db).get_set(set_id, organization_id=org_id)
    ann_service = AnnotationService(db)

    features: list[dict[str, Any]] = []
    offset = 0
    total = 0
    while True:
        items, total = await ann_service.list_annotations(
            set_id=set_id,
            limit=EXPORT_PAGE_SIZE,
            offset=offset,
            organization_id=org_id,
        )
        if not items:
            break
        for ann in items:
            geom = serialize_geometry(ann.geometry)
            if geom is None:
                continue
            features.append({
                "type": "Feature",
                "id": str(ann.id),
                "geometry": geom,
                "properties": {
                    "class_id": str(ann.class_id),
                    "confidence": ann.confidence,
                    **(ann.properties or {}),
                },
            })
        offset += len(items)
        if offset >= total:
            break

    body = {
        "type": "FeatureCollection",
        "features": features,
        "total": total,
        "annotation_set_id": str(annotation_set.id),
        "annotation_set_name": annotation_set.name,
    }
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    filename = f"annotation-set-{annotation_set.id}.geojson"
    s3_key = f"exports/annotation-sets/{annotation_set.id}/{uuid4().hex}/{filename}"

    def _upload_and_sign() -> str:
        storage_service.ensure_org_bucket(org_id)
        storage_service.upload_bytes(
            org_id,
            s3_key,
            body_bytes,
            content_type="application/geo+json",
        )
        return storage_service.generate_download_url(org_id, s3_key, payload.ttl_seconds)

    try:
        download_url = await asyncio.to_thread(_upload_and_sign)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not export annotation set: {exc}") from exc

    await log_audit_event(
        action="annotation_sets.export", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(set_id), session=db,
    )
    return AnnotationSetExportResponse(
        annotation_set_id=annotation_set.id,
        format=payload.format,
        filename=filename,
        s3_key=s3_key,
        download_url=download_url,
        expires_in=payload.ttl_seconds,
    )


@set_router.get("/raster/values", response_model=RasterMaskValuesPreviewRead)
async def preview_raster_mask_values(
    set_id: UUID,
    dataset_item_id: str,
    band_index: int = Query(default=1, ge=1),
    max_values: int = Query(default=256, ge=1, le=2048),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    await AnnotationSetService(db).get_set(set_id, organization_id=org_id)
    item = await _get_dataset_item_for_org(db, dataset_item_id, org_id)
    try:
        async with _raster_preview_semaphore:
            preview = await asyncio.to_thread(
                extract_unique_values, item.s3_uri, _gdal_env_for_api(),
                band_index=band_index, max_values=max_values,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MemoryError:
        raise HTTPException(status_code=507, detail="Raster too large to preview")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read raster values: {exc}") from exc
    return RasterMaskValuesPreviewRead(
        dataset_item_id=item.id, band_index=band_index,
        values=preview["values"], total_unique=preview["total_unique"], truncated=preview["truncated"],
    )


@set_router.get("/raster/config", response_model=RasterMaskConfigRead)
async def get_raster_mask_config(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    annotation_set = await AnnotationSetService(db).get_set(set_id, organization_id=org_id)
    cfg = annotation_set.raster_config
    if not cfg:
        raise HTTPException(status_code=404, detail="This annotation set has no raster config")
    tile_url_template = (
        f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/tiles/raster-masks/{annotation_set.id}/{{z}}/{{x}}/{{y}}.png"
    )
    return RasterMaskConfigRead(
        annotation_set_id=annotation_set.id,
        map_layer_id=None,
        dataset_item_id=UUID(cfg["dataset_item_id"]),
        dataset_id=UUID(cfg["dataset_id"]),
        stac_collection_id=cfg["stac_collection_id"],
        stac_item_id=cfg["stac_item_id"],
        band_index=cfg["band_index"],
        nodata_value=cfg.get("nodata_value"),
        value_class_map={k: UUID(v) for k, v in (cfg.get("value_class_map") or {}).items()},
        colormap=cfg.get("colormap", {}),
        tile_url_template=tile_url_template,
    )


@set_router.patch("/raster/config", response_model=RasterMaskConfigRead)
async def configure_raster_mask_for_annotation_set(
    set_id: UUID,
    payload: RasterMaskConfigUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.get_set(set_id, organization_id=org_id)
    if annotation_set.schema_id is None:
        raise HTTPException(status_code=400, detail="Annotation set must have schema_id for raster class mapping")

    schema_exists = await db.execute(
        select(AnnotationSchema.id).where(
            AnnotationSchema.id == annotation_set.schema_id,
            AnnotationSchema.organization_id == org_id,
            AnnotationSchema.deleted_at.is_(None),
        )
    )
    if schema_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Annotation schema not found")

    item = await _get_dataset_item_for_org(db, payload.dataset_item_id, org_id)
    value_class_map = _coerce_value_map(payload.value_class_map)
    colormap = await _build_colormap(
        db, schema_id=annotation_set.schema_id,
        value_class_map=value_class_map, nodata_value=payload.nodata_value,
    )

    annotation_set.dataset_id = item.dataset_id
    annotation_set.dataset_item_id = item.id
    annotation_set.raster_config = {
        "dataset_item_id": str(item.id),
        "dataset_id": str(item.dataset_id),
        "stac_collection_id": item.stac_collection_id,
        "stac_item_id": item.stac_item_id,
        "asset": "data",
        "band_index": payload.band_index,
        "nodata_value": payload.nodata_value,
        "value_class_map": {k: str(v) for k, v in value_class_map.items()},
        "colormap": colormap,
    }
    await db.commit()

    await log_audit_event(
        action="annotation_sets.raster_config.update", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(set_id), session=db,
    )
    tile_url_template = (
        f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/tiles/raster-masks/{annotation_set.id}/{{z}}/{{x}}/{{y}}.png"
    )
    return RasterMaskConfigRead(
        annotation_set_id=annotation_set.id,
        map_layer_id=payload.map_layer_id,
        dataset_item_id=item.id,
        dataset_id=item.dataset_id,
        stac_collection_id=item.stac_collection_id,
        stac_item_id=item.stac_item_id,
        band_index=payload.band_index,
        nodata_value=payload.nodata_value,
        value_class_map=value_class_map,
        colormap=colormap,
        tile_url_template=tile_url_template,
    )


@set_router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_annotations_from_geojson(
    set_id: UUID,
    payload: GeoJSONImportRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    annotation_set = await AnnotationSetService(db).get_set(set_id, organization_id=org_id)
    if annotation_set.schema_id is None:
        raise HTTPException(status_code=400, detail="Annotation set has no schema_id; cannot resolve classes")

    body_bytes = json.dumps(payload.geojson, separators=(",", ":")).encode("utf-8")
    if len(body_bytes) > MAX_IMPORT_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"GeoJSON exceeds {MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB limit")
    if payload.geojson.get("type") != "FeatureCollection":
        raise HTTPException(status_code=400, detail="geojson must be a FeatureCollection")

    filename = payload.filename or f"import-{uuid4().hex}.geojson"
    job_uuid = uuid4()
    s3_key = f"imports/annotations/{job_uuid}/{filename}"

    def _upload() -> None:
        storage_service.ensure_org_bucket(org_id)
        storage_service.upload_bytes(
            org_id,
            s3_key,
            body_bytes,
            content_type="application/geo+json",
        )

    try:
        await asyncio.to_thread(_upload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not stage import file: {exc}") from exc

    job = Job(
        id=job_uuid, organization_id=org_id,
        type=JobType.IMPORT_ANNOTATIONS, status=JobStatus.QUEUED,
        created_by_user_id=current_user.id,
        config={
            "annotation_set_id": str(set_id), "s3_key": s3_key, "filename": filename,
            "default_class_id": str(payload.default_class_id) if payload.default_class_id else None,
            "class_property": payload.class_property, "confidence_property": payload.confidence_property,
        },
        total_items=0, processed_items=0, failed_items=0, progress=0.0,
    )
    db.add(job)
    await db.commit()

    await log_audit_event(
        action="annotation_sets.import", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="annotation_set", entity_id=str(set_id), session=db,
    )

    from app.workers.bulk.tasks import bulk_import_annotations  # noqa: PLC0415
    from app.workers.queues import BULK  # noqa: PLC0415

    bulk_import_annotations.apply_async(args=[str(job.id)], queue=BULK)
    return {"job_id": str(job.id), "status": JobStatus.QUEUED.value}


# ── Project-scoped (/projects/{project_id}/annotation-sets) ──────────────────

@project_router.get("", response_model=AnnotationSetListResponse)
async def list_project_annotation_sets(
    project_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_project_sets(project_id, org_id, limit, offset)
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@project_router.post("/link", response_model=AnnotationSetProjectLinkRead)
async def link_annotation_set_to_project(
    project_id: UUID,
    payload: AnnotationSetLinkRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    link = await service.link_set_to_project(
        project_id=project_id, annotation_set_id=payload.annotation_set_id,
        organization_id=org_id, linked_by=current_user.id,
    )
    await log_audit_event(
        action="annotation_sets.project_link", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="project_annotation_set",
        entity_id=f"{project_id}:{payload.annotation_set_id}", session=db,
    )
    return AnnotationSetProjectLinkRead.model_validate(link)


@project_router.delete("/{set_id}/unlink", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_annotation_set_from_project(
    project_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await AnnotationSetService(db).unlink_set_from_project(project_id, set_id, org_id)
    await log_audit_event(
        action="annotation_sets.project_unlink", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="project_annotation_set",
        entity_id=f"{project_id}:{set_id}", session=db,
    )


# ── Map-scoped (/maps/{map_id}/annotation-sets) ───────────────────────────────

@map_router.get("", response_model=AnnotationSetMountListResponse)
async def list_map_annotation_set_mounts(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_map_mounts(map_id, org_id)
    return AnnotationSetMountListResponse(
        items=[AnnotationSetMountRead.model_validate(item) for item in items], total=total,
    )


@map_router.post("/mount", response_model=AnnotationSetMountRead)
async def mount_annotation_set_on_map(
    map_id: UUID,
    payload: AnnotationSetMountRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    mount = await AnnotationSetService(db).mount_set_on_map(map_id, payload, org_id)
    await log_audit_event(
        action="annotation_sets.map_mount", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="map_annotation_set",
        entity_id=f"{map_id}:{payload.annotation_set_id}", session=db,
    )
    return AnnotationSetMountRead.model_validate(mount)


@map_router.patch("/{set_id}", response_model=AnnotationSetMountRead)
async def update_map_annotation_set_mount(
    map_id: UUID,
    set_id: UUID,
    payload: AnnotationSetMountUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    mount = await AnnotationSetService(db).update_map_mount(map_id, set_id, payload, org_id)
    await log_audit_event(
        action="annotation_sets.map_mount_update", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="map_annotation_set",
        entity_id=f"{map_id}:{set_id}", session=db,
    )
    return AnnotationSetMountRead.model_validate(mount)


@map_router.delete("/{set_id}/unmount", status_code=status.HTTP_204_NO_CONTENT)
async def unmount_annotation_set_from_map(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await AnnotationSetService(db).unmount_set_from_map(map_id, set_id, org_id)
    await log_audit_event(
        action="annotation_sets.map_unmount", actor_id=str(current_user.id),
        organization_id=str(org_id), entity="map_annotation_set",
        entity_id=f"{map_id}:{set_id}", session=db,
    )
