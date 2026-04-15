import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.enums import JobStatus, JobType
from app.core.geometry import serialize_geometry
from app.models.job import Job
from app.models.user import User

logger = logging.getLogger(__name__)

# Module-level httpx client to Martin (connection pooled).
_martin_client: httpx.AsyncClient | None = None


def _get_martin_client() -> httpx.AsyncClient:
    global _martin_client  # noqa: PLW0603
    if _martin_client is None or _martin_client.is_closed:
        _martin_client = httpx.AsyncClient(base_url=settings.MARTIN_URL, timeout=15.0)
    return _martin_client
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetListResponse,
    AnnotationSetRead,
    AnnotationSetUpdate,
)
from app.services import storage_service
from app.services.annotation_service import AnnotationService
from app.services.annotation_set_service import AnnotationSetService

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


# ── Martin vector tile proxy ─────────────────────────────────────────────────


@set_router.get("/tiles/{z}/{x}/{y}.pbf")
async def proxy_annotation_set_tile(
    set_id: UUID,
    z: int,
    x: int,
    y: int,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Response:
    """Stream a vector tile for ``set_id`` from Martin.

    Verifies that the calling org owns the annotation set, then forwards
    the request to Martin's ``annotation_set_mvt`` function source.  Martin
    runs as ``martin_reader`` (BYPASSRLS) so this proxy is the only place
    where org isolation is enforced for annotation tiles.
    """
    # RLS-enforced existence check.  AnnotationSetService.get_set goes
    # through the schema/map → org join, so a 404 here means the caller's
    # org does not own the set.
    set_service = AnnotationSetService(db)
    await set_service.get_set(set_id, organization_id=org_id)

    client = _get_martin_client()
    try:
        upstream = await client.get(
            f"/annotation_set_mvt/{z}/{x}/{y}",
            params={"set_id": str(set_id)},
        )
    except httpx.RequestError as exc:
        logger.error("martin_proxy_error set_id=%s error=%s", set_id, exc)
        raise HTTPException(status_code=502, detail="Tile service unavailable") from exc

    if upstream.status_code == 204:
        return Response(status_code=204)
    if upstream.status_code >= 400:
        body_preview = upstream.text[:300] if upstream.text else "(empty)"
        logger.warning(
            "martin_upstream_error set_id=%s status=%s body=%s",
            set_id, upstream.status_code, body_preview,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Tile service error (HTTP {upstream.status_code})",
        )

    headers: dict[str, str] = {}
    for header in ("cache-control", "etag", "last-modified", "content-encoding"):
        if header in upstream.headers:
            headers[header] = upstream.headers[header]

    return Response(
        content=upstream.content,
        status_code=200,
        media_type="application/x-protobuf",
        headers=headers,
    )
