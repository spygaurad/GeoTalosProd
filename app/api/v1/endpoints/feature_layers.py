import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import Response
from geoalchemy2.shape import from_shape
from shapely.geometry import shape
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.feature_layer import (
    FeatureLayerBulkCreate,
    FeatureLayerCreate,
    FeatureLayerListResponse,
    FeatureLayerRead,
)
from app.models.feature_layer import FeatureLayer
from app.services.feature_layer_service import FeatureLayerService
from app.services.tile_proxy import forward_tile, gzip_if_accepted
from app.services.vector_upload import MAX_UPLOAD_BYTES, UploadError, parse_upload

router = APIRouter(prefix="/feature-layers", tags=["feature-layers"])


# ── Uploaded vector layers: upload, list, tiles, delete ───────────────────────
# A "layer" is every feature_layers row of one organization with the same layer_name.
# These routes come before /{feature_id} so "layers" is not read as an id.

_LAYER_NAME = re.compile(r"^[\w .()-]{1,120}$")


def _clean_layer_name(raw: str) -> str:
    name = raw.strip()
    if not _LAYER_NAME.fullmatch(name):
        raise HTTPException(status_code=422, detail="Layer name: 1 to 120 letters, numbers, spaces, . ( ) _ -")
    return name


@router.get("/layers")
async def list_vector_layers(
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The organization's uploaded layers with feature count, bounds, geometry types and attributes."""
    rows = (
        await db.execute(
            text(
                "SELECT layer_name, count(*) AS n, ST_XMin(ST_Extent(geometry)), ST_YMin(ST_Extent(geometry)), "
                "ST_XMax(ST_Extent(geometry)), ST_YMax(ST_Extent(geometry)), "
                "array_agg(DISTINCT GeometryType(geometry)), max(created_at) "
                "FROM feature_layers WHERE organization_id = :org GROUP BY layer_name ORDER BY max(created_at) DESC"
            ),
            {"org": org_id},
        )
    ).all()
    items = []
    for name, n, x0, y0, x1, y1, types, created in rows:
        keys = (
            await db.execute(
                text(
                    "SELECT DISTINCT jsonb_object_keys(properties) AS k FROM "
                    "(SELECT properties FROM feature_layers WHERE organization_id = :org AND layer_name = :l "
                    "AND jsonb_typeof(properties) = 'object' LIMIT 200) s ORDER BY k"
                ),
                {"org": org_id, "l": name},
            )
        ).scalars().all()
        items.append(
            {
                "name": name,
                "feature_count": n,
                "bounds": [x0, y0, x1, y1],
                "geometry_types": sorted(t.lower() for t in types),
                "attributes": list(keys),
                "created_at": created,
                "tile_url": f"/api/v1/feature-layers/layers/{quote(name, safe='')}/tiles/{{z}}/{{x}}/{{y}}.pbf",
                "tile_layer": "feature_layer_mvt",
            }
        )
    return {"items": items}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_vector_layer(
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255, description="Original file name; its extension picks the format"),
    layer_name: str | None = Query(None),
    crs: str | None = Query(None),
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Add a layer from a GeoJSON or CSV file sent as the raw request body (no multipart, so no
    extra dependency). ``crs`` is the file's CRS if it is not EPSG:4326."""
    name = _clean_layer_name(layer_name or filename.rsplit(".", 1)[0])
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 50 MB")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:  # a chunked upload has no content-length to check up front
            raise HTTPException(status_code=413, detail="File is larger than 50 MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        features, skipped, reasons = parse_upload(filename, data, crs=crs)
    except UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    exists = await db.scalar(
        text("SELECT 1 FROM feature_layers WHERE organization_id = :org AND layer_name = :l LIMIT 1"),
        {"org": org_id, "l": name},
    )
    if exists:
        raise HTTPException(status_code=409, detail=f'A layer named "{name}" already exists. Delete it or pick another name.')

    for start in range(0, len(features), 2000):
        db.add_all(
            FeatureLayer(
                organization_id=org_id,
                layer_name=name,
                geometry=from_shape(shape(f["geometry"]), srid=4326),
                properties=f["properties"],
                created_by=current_user.id,
            )
            for f in features[start : start + 2000]
        )
        await db.flush()
    await db.commit()
    await log_audit_event(
        action="feature_layers.upload",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        extra={"layer": name, "count": len(features), "skipped": skipped},
        session=db,
    )
    return {"layer": name, "created": len(features), "skipped": skipped, "reasons": reasons}


@router.get("/layers/{layer_name}/tiles/{z}/{x}/{y}.pbf", response_class=Response)
async def get_vector_layer_tile(
    layer_name: str,
    request: Request,
    z: int = Path(..., ge=0, le=22),
    x: int = Path(..., ge=0),
    y: int = Path(..., ge=0),
    org_id: UUID = Depends(require_org_role("org:viewer")),
) -> Response:
    if x >= 2**z or y >= 2**z:
        raise HTTPException(status_code=422, detail="Tile coordinates are outside the zoom level bounds")
    # org_id comes from the caller's session, so one organization can never read another's layer.
    response = await forward_tile(
        request.app.state.martin_client,
        f"/feature_layer_mvt/{z}/{x}/{y}?org_id={org_id}&layer={quote(layer_name, safe='')}",
        default_content_type="application/x-protobuf",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return gzip_if_accepted(request, response)


@router.delete("/layers/{layer_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vector_layer(
    layer_name: str,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        text("DELETE FROM feature_layers WHERE organization_id = :org AND layer_name = :l"),
        {"org": org_id, "l": layer_name},
    )
    await db.commit()
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="Layer not found")
    await log_audit_event(
        action="feature_layers.delete_layer",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        extra={"layer": layer_name, "count": result.rowcount},
        session=db,
    )


@router.get("", response_model=FeatureLayerListResponse)
async def list_feature_layers(
    layer_name: str | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    items, total = await service.list_features(
        limit=limit, offset=offset, organization_id=org_id, layer_name=layer_name
    )
    read_items = [FeatureLayerRead(**service.to_read_dict(f)) for f in items]
    return FeatureLayerListResponse(items=read_items, total=total, limit=limit, offset=offset)


@router.get("/{feature_id}", response_model=FeatureLayerRead)
async def get_feature_layer(
    feature_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    feature = await service.get_feature(feature_id, organization_id=org_id)
    return FeatureLayerRead(**service.to_read_dict(feature))


@router.post("", response_model=FeatureLayerRead, status_code=status.HTTP_201_CREATED)
async def create_feature_layer(
    payload: FeatureLayerCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    feature = await service.create_feature(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="feature_layers.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        entity_id=str(feature.id),
        session=db,
    )
    return FeatureLayerRead(**service.to_read_dict(feature))


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_feature_layers(
    payload: FeatureLayerBulkCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    created = await service.bulk_create_features(
        payload.features, organization_id=org_id, created_by=current_user.id
    )
    await log_audit_event(
        action="feature_layers.bulk_create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        extra={"count": created},
        session=db,
    )
    return {"created": created}


@router.delete("/{feature_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_layer(
    feature_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    await service.delete_feature(feature_id, organization_id=org_id)
    await log_audit_event(
        action="feature_layers.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        entity_id=str(feature_id),
        session=db,
    )
