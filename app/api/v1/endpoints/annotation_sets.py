import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
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
from app.models.map import Map
from app.models.map_layer import MapLayer
from app.models.project import Project
from app.models.user import User
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetListResponse,
    AnnotationSetRead,
    RasterMaskConfigRead,
    RasterMaskConfigUpdate,
    RasterMaskValuesPreviewRead,
    AnnotationSetUpdate,
)
from app.services import storage_service
from app.services.annotation_service import AnnotationService
from app.services.annotation_set_service import AnnotationSetService
from app.workers.ingestion.rasterio_utils import extract_unique_values

logger = logging.getLogger(__name__)

# Semaphore to limit concurrent rasterio S3 reads (prevents thread pool exhaustion
# when many users trigger preview scans simultaneously).
_raster_preview_semaphore = asyncio.Semaphore(2)

# Cap the size of an inline-uploaded GeoJSON document (50 MB).  Larger
# imports should be split.
MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024


class GeoJSONImportRequest(BaseModel):
    """Inline GeoJSON FeatureCollection import payload."""

    geojson: dict[str, Any] = Field(
        ..., description="A GeoJSON FeatureCollection (parsed as a JSON object)"
    )
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Optional original filename — used for the audit trail and S3 key",
    )
    default_class_id: UUID | None = Field(
        default=None,
        description="Fallback class UUID for features whose class cannot be resolved",
    )
    class_property: str = Field(
        default="class_id",
        max_length=64,
        description="Feature property name carrying the class UUID",
    )
    confidence_property: str | None = Field(
        default=None,
        max_length=64,
        description="Optional feature property name carrying a numeric confidence",
    )


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
        # Network timeout for preview reads — prevents hanging on slow/unavailable files.
        "GDAL_HTTP_TIMEOUT": "30",
        "CPL_CURL_GZIP": "YES",
    }


def _hex_to_rgba(value: str) -> list[int]:
    """Parse a CSS hex color string into [R, G, B, A] components.

    Raises ``ValueError`` if the string is not a valid 3-, 6-, or 8-digit hex
    color so callers can decide whether to log a warning or surface an error.
    """
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 6:
        raw = f"{raw}ff"
    if len(raw) != 8:
        raise ValueError(f"Invalid hex color (expected #RGB, #RRGGBB, or #RRGGBBAA): {value!r}")
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
            logger.warning(
                "class_fill_invalid_hex class_id=%s value=%r — defaulting to white",
                annotation_class.id, fill,
            )
    return [255, 255, 255, 255]


def _coerce_value_map(raw_map: dict[str, UUID]) -> dict[str, UUID]:
    """Normalize pixel-value → class-UUID keys to canonical string form.

    Integer-valued floats (``"3.0"``) are stored as ``"3"``; genuine floats
    (``"3.7"``) are preserved as-is so float-dtype rasters round-trip correctly.
    """
    coerced: dict[str, UUID] = {}
    for raw_key, cls_id in raw_map.items():
        key = str(raw_key).strip()
        try:
            float_val = float(key)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid raster class value key: {raw_key!r}",
            ) from exc
        # Use integer representation only when the value is exactly an integer.
        normalized = str(int(float_val)) if float_val == int(float_val) else str(float_val)
        coerced[normalized] = cls_id
    return coerced


async def _get_dataset_item_for_org(
    db: AsyncSession, dataset_item_ref: str | UUID, org_id: UUID
) -> DatasetItem:
    dataset_item_text = str(dataset_item_ref).strip()
    filters = [
        DatasetItem.organization_id == org_id,
    ]
    # Only treat as a UUID if it has the canonical dashed format (36 chars: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).
    # Python's uuid.UUID() accepts 32-char hex strings without dashes, which would cause stac_item_ids
    # that happen to be 32-char hex strings to be misinterpreted as primary key UUIDs → 404.
    if len(dataset_item_text) == 36 and dataset_item_text.count('-') == 4:
        try:
            dataset_item_uuid = UUID(dataset_item_text)
            filters.append(DatasetItem.id == dataset_item_uuid)
        except ValueError:
            filters.append(DatasetItem.stac_item_id == dataset_item_text)
    else:
        filters.append(DatasetItem.stac_item_id == dataset_item_text)

    result = await db.execute(select(DatasetItem).where(*filters))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found")
    return item


async def _get_map_layer_for_org(
    db: AsyncSession, map_layer_id: UUID, set_id: UUID, org_id: UUID
) -> MapLayer:
    result = await db.execute(
        select(MapLayer)
        .join(Map, Map.id == MapLayer.map_id)
        .join(Project, Project.id == Map.project_id)
        .where(
            MapLayer.id == map_layer_id,
            MapLayer.annotation_set_id == set_id,
            Project.organization_id == org_id,
        )
    )
    layer = result.scalar_one_or_none()
    if layer is None:
        raise HTTPException(status_code=404, detail="Map layer not found for this annotation set")
    return layer


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
        .where(
            AnnotationClass.schema_id == schema_id,
            AnnotationClass.id.in_(class_ids),
        )
    )
    classes = {row.id: row for row in rows.scalars().all()}
    if len(classes) != len(class_ids):
        raise HTTPException(status_code=400, detail="One or more class IDs do not belong to the annotation set schema")

    colormap: dict[str, list[int]] = {}
    for value, cls_id in value_class_map.items():
        colormap[value] = _extract_class_fill_rgba(classes[cls_id])

    if nodata_value is not None:
        # Normalize nodata key the same way as value_class_map keys — integer
        # floats become "3", genuine floats stay "3.7".
        fv = float(nodata_value)
        nodata_key = str(int(fv)) if fv == int(fv) else str(fv)
        colormap[nodata_key] = [0, 0, 0, 0]
    return colormap

router = APIRouter(prefix="/maps/{map_id}/annotation-sets", tags=["annotation-sets"])

# Standalone router for annotation-set operations by ID (no map_id in path)
set_router = APIRouter(prefix="/annotation-sets/{set_id}", tags=["annotation-sets"])

# Standalone router for map-independent annotation set creation
standalone_router = APIRouter(prefix="/annotation-sets", tags=["annotation-sets"])


@standalone_router.post("", response_model=AnnotationSetRead, status_code=status.HTTP_201_CREATED)
async def create_standalone_annotation_set(
    payload: AnnotationSetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Create an annotation set without a parent map.

    Used for grouping annotations that share a purpose / source / context
    rather than belonging to a specific map view (e.g. a GeoJSON import).
    Either ``schema_id`` or ``map_id`` must be provided as the org anchor.
    """
    service = AnnotationSetService(db)
    annotation_set = await service.create_set(
        payload,
        organization_id=org_id,
        created_by_user_id=current_user.id,
    )
    await log_audit_event(
        action="annotation_sets.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(annotation_set.id),
        session=db,
    )
    return annotation_set

# Project-scoped router for listing annotation sets across all maps in a project
project_router = APIRouter(prefix="/projects/{project_id}/annotation-sets", tags=["annotation-sets"])


@router.get("", response_model=AnnotationSetListResponse)
async def list_annotation_sets(
    map_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_sets(
        map_id=map_id, limit=limit, offset=offset, organization_id=org_id
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{set_id}", response_model=AnnotationSetRead)
async def get_annotation_set(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    return await service.get_set(set_id, organization_id=org_id, map_id=map_id)


@router.post("", response_model=AnnotationSetRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_set(
    map_id: UUID,
    payload: AnnotationSetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.map_id != map_id:
        raise HTTPException(status_code=400, detail="map_id in payload must match path")
    service = AnnotationSetService(db)
    annotation_set = await service.create_set(
        payload,
        organization_id=org_id,
        created_by_user_id=current_user.id,
    )
    await log_audit_event(
        action="annotation_sets.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(annotation_set.id),
        session=db,
    )
    return annotation_set


@router.patch("/{set_id}", response_model=AnnotationSetRead)
async def update_annotation_set(
    map_id: UUID,
    set_id: UUID,
    payload: AnnotationSetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.update_set(
        set_id, payload, organization_id=org_id, map_id=map_id
    )
    await log_audit_event(
        action="annotation_sets.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )
    return annotation_set


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_set(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.delete_set(set_id, organization_id=org_id, map_id=map_id)
    await log_audit_event(
        action="annotation_sets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )


# ── Project-scoped annotation set list ──────────────────────────────────────


@project_router.get("", response_model=AnnotationSetListResponse)
async def list_project_annotation_sets(
    project_id: UUID,
    dataset_id: UUID | None = None,
    stac_item_id: str | None = None,
    unattached: bool = False,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """List annotation sets across a project.

    Optional filters:
      - dataset_id: only sets attached to this dataset
      - stac_item_id: only sets attached to this dataset item
      - unattached: only standalone sets (no map, no dataset)
    """
    service = AnnotationSetService(db)
    items, total = await service.list_by_project(
        organization_id=org_id,
        limit=limit,
        offset=offset,
        project_id=project_id,
        dataset_id=dataset_id,
        stac_item_id=stac_item_id,
        unattached=unattached,
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@standalone_router.get("", response_model=AnnotationSetListResponse)
async def list_org_annotation_sets(
    dataset_id: UUID | None = None,
    stac_item_id: str | None = None,
    unattached: bool = False,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """List annotation sets visible to the organization.

    Annotation sets may be project-independent (schema-only anchored); this
    endpoint returns both map-anchored and schema-anchored sets scoped to the
    caller's org. Supports the same filters as the project-scoped variant.
    """
    service = AnnotationSetService(db)
    items, total = await service.list_by_project(
        organization_id=org_id,
        limit=limit,
        offset=offset,
        dataset_id=dataset_id,
        stac_item_id=stac_item_id,
        unattached=unattached,
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


# ── Standalone set endpoints (by set_id, no map_id) ───────────────────────────


@set_router.get("", response_model=AnnotationSetRead)
async def get_annotation_set_standalone(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    return await service.get_set(set_id, organization_id=org_id)


@set_router.patch("", response_model=AnnotationSetRead)
async def update_annotation_set_standalone(
    set_id: UUID,
    payload: AnnotationSetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.update_set(set_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotation_sets.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )
    return annotation_set


@set_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_set_standalone(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.delete_set(set_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_sets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )


@set_router.get("/bounds")
async def get_annotation_set_bounds(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return the WGS-84 bounding box of all annotations in this set, or null if empty."""
    set_service = AnnotationSetService(db)
    await set_service.get_set(set_id, organization_id=org_id)

    row = (await db.execute(
        text("""
            SELECT
                ST_XMin(ST_Extent(geometry)) AS west,
                ST_YMin(ST_Extent(geometry)) AS south,
                ST_XMax(ST_Extent(geometry)) AS east,
                ST_YMax(ST_Extent(geometry)) AS north
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
    """Return annotations in this set as a GeoJSON FeatureCollection."""
    ann_service = AnnotationService(db)
    items, total = await ann_service.list_annotations(
        set_id=set_id, limit=limit, offset=offset, organization_id=org_id
    )

    features = []
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

    return {
        "type": "FeatureCollection",
        "features": features,
        "total": total,
    }


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
    set_service = AnnotationSetService(db)
    await set_service.get_set(set_id, organization_id=org_id)
    item = await _get_dataset_item_for_org(db, dataset_item_id, org_id)

    try:
        async with _raster_preview_semaphore:
            preview = await asyncio.to_thread(
                extract_unique_values,
                item.s3_uri,
                _gdal_env_for_api(),
                band_index=band_index,
                max_values=max_values,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MemoryError:
        raise HTTPException(
            status_code=507,
            detail="Raster is too large to preview; try a smaller max_values or a decimated file",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not read raster values: {exc}") from exc

    return RasterMaskValuesPreviewRead(
        dataset_item_id=item.id,
        band_index=band_index,
        values=preview["values"],
        total_unique=preview["total_unique"],
        truncated=preview["truncated"],
    )


@set_router.get("/raster/config", response_model=RasterMaskConfigRead)
async def get_raster_mask_config(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    """Return the saved raster config for a segmentation mask annotation set.

    Returns 404 if the annotation set has no raster config (i.e. it is a
    vector annotation set, not a raster mask).
    """
    set_service = AnnotationSetService(db)
    annotation_set = await set_service.get_set(set_id, organization_id=org_id)

    cfg = annotation_set.raster_config
    if not cfg:
        raise HTTPException(
            status_code=404,
            detail="This annotation set has no raster config; it is not a segmentation mask",
        )

    tile_url_template = (
        f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/tiles/raster-masks/"
        f"{annotation_set.id}/{{z}}/{{x}}/{{y}}.png"
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
    set_service = AnnotationSetService(db)
    annotation_set = await set_service.get_set(set_id, organization_id=org_id)
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
        db,
        schema_id=annotation_set.schema_id,
        value_class_map=value_class_map,
        nodata_value=payload.nodata_value,
    )

    annotation_set.dataset_id = item.dataset_id
    annotation_set.stac_item_id = item.stac_item_id

    raster_cfg_data = {
        "dataset_item_id": str(item.id),
        "dataset_id": str(item.dataset_id),
        "stac_collection_id": item.stac_collection_id,
        "stac_item_id": item.stac_item_id,
        "asset": "data",
        "band_index": payload.band_index,
        "nodata_value": payload.nodata_value,
        "value_class_map": {k: str(v) for k, v in value_class_map.items()},
        # Persist colormap so the tile endpoint never needs to recompute it.
        "colormap": colormap,
    }

    # Always persist on the annotation set itself — this is the primary storage
    # used by the tile proxy endpoint (no MapLayer required to serve tiles).
    annotation_set.raster_config = raster_cfg_data

    map_layer_id = payload.map_layer_id
    if map_layer_id is not None:
        layer = await _get_map_layer_for_org(db, map_layer_id, set_id, org_id)
        source_config = dict(layer.source_config or {})
        source_config["raster_mask"] = raster_cfg_data
        layer.source_config = source_config

    await db.commit()

    await log_audit_event(
        action="annotation_sets.raster_config.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )

    # New dedicated tile endpoint applies the colormap server-side, so the
    # frontend never needs to embed a bulky colormap in every tile URL.
    tile_url_template = (
        f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/tiles/raster-masks/"
        f"{annotation_set.id}/{{z}}/{{x}}/{{y}}.png"
    )
    return RasterMaskConfigRead(
        annotation_set_id=annotation_set.id,
        map_layer_id=map_layer_id,
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


# ── GeoJSON import (async job) ───────────────────────────────────────────────


@set_router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_annotations_from_geojson(
    set_id: UUID,
    payload: GeoJSONImportRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Import annotations into ``set_id`` from an inline GeoJSON document.

    The frontend should ``JSON.parse`` the user-selected ``.geojson`` file
    and POST it as the ``geojson`` field of this request body (the file
    itself is not uploaded as multipart).

    Each feature's ``properties[class_property]`` must hold a class UUID
    that belongs to the set's schema.  Features whose class cannot be
    resolved fall back to ``default_class_id`` (and are counted in
    ``unmapped_count``); features with no resolvable class and no default
    are skipped and reported in the job's error sample.

    Returns 202 immediately with ``{"job_id": "..."}``; poll
    ``GET /api/v1/jobs/{job_id}`` for progress and the final result summary
    (stored in ``config.result``).
    """
    set_service = AnnotationSetService(db)
    annotation_set = await set_service.get_set(set_id, organization_id=org_id)
    if annotation_set.schema_id is None:
        raise HTTPException(
            status_code=400,
            detail="Annotation set has no schema_id; cannot resolve classes",
        )

    # Re-serialize and size-cap the GeoJSON before staging in S3.
    body_bytes = json.dumps(payload.geojson, separators=(",", ":")).encode("utf-8")
    if len(body_bytes) > MAX_IMPORT_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"GeoJSON exceeds {MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB limit",
        )
    if payload.geojson.get("type") != "FeatureCollection":
        raise HTTPException(
            status_code=400, detail="geojson must be a FeatureCollection"
        )

    filename = payload.filename or f"import-{uuid4().hex}.geojson"
    # Predictable, immutable S3 key — one per import job.
    job_uuid = uuid4()
    s3_key = f"imports/annotations/{job_uuid}/{filename}"

    # Make sure the org bucket exists, then upload as a single PUT.
    def _upload() -> None:
        storage_service.ensure_org_bucket(org_id)
        client = storage_service._s3_client()
        client.put_object(
            Bucket=storage_service.bucket_name(org_id),
            Key=s3_key,
            Body=body_bytes,
            ContentType="application/geo+json",
        )

    try:
        await asyncio.to_thread(_upload)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not stage import file: {exc}"
        ) from exc

    # Create the job row in QUEUED state and enqueue the worker after commit.
    job = Job(
        id=job_uuid,
        organization_id=org_id,
        type=JobType.IMPORT_ANNOTATIONS,
        status=JobStatus.QUEUED,
        created_by_user_id=current_user.id,
        config={
            "annotation_set_id": str(set_id),
            "s3_key": s3_key,
            "filename": filename,
            "default_class_id": str(payload.default_class_id) if payload.default_class_id else None,
            "class_property": payload.class_property,
            "confidence_property": payload.confidence_property,
        },
        total_items=0,
        processed_items=0,
        failed_items=0,
        progress=0.0,
    )
    db.add(job)
    await db.commit()

    await log_audit_event(
        action="annotation_sets.import",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )

    # Import here to avoid pulling Celery / rasterio at module load time.
    from app.workers.bulk.tasks import bulk_import_annotations  # noqa: PLC0415
    from app.workers.queues import BULK  # noqa: PLC0415

    bulk_import_annotations.apply_async(args=[str(job.id)], queue=BULK)

    return {"job_id": str(job.id), "status": JobStatus.QUEUED.value}
