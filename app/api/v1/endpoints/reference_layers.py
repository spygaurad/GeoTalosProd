"""Public reference layers — global, read-only.

Served by Martin (see infra/docker/martin/config.yaml): small layers live in PostGIS
(the `reference` schema), large ones are Tippecanoe PMTiles in the MinIO
reference-tiles bucket. Build and load scripts: scripts/reference_layers/.
They are the same for every organization, so there is no org check and the
tiles are publicly cacheable. Callers still need a signed-in session
(ClerkAuthMiddleware). Only names in REFERENCE_LAYERS are forwarded, so this
route can never reach the private annotation sources on the same Martin.
"""

import logging

import json
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.models.user import User
from app.services.reference_layer_meta import (
    DENSITY_SOURCES,
    LAYER_META,
    LayerStyle,
    check_style_for_layer,
)
from app.services.tile_proxy import forward_tile, gzip_if_accepted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reference-layers", tags=["reference-layers"])

# Martin source ids exposed to the frontend. Add one entry per layer. A layer whose
# source is missing in Martin (not built or uploaded yet) is skipped in the list and
# returns 404 for tiles, so this can name layers before they exist.
REFERENCE_LAYERS: tuple[str, ...] = (
    "ramsar",  # PostGIS, reference.ramsar
    "cofopri",  # PMTiles from here on
    "raisg_protected_areas",
    "raisg_mining",
    "gfw_mining",
    "gfw_logging",
    "hydrorivers",
    "mrds",
    "osm_roads",
)

# Static tilesets only change when rebuilt, and the rebuild is deployed together
# with a restart, so one day is a safe browser cache lifetime.
_CACHE_CONTROL = "public, max-age=86400"


def _require_layer(name: str) -> None:
    # Heatmap point tilesets are served like layers but never listed on their own.
    if name not in REFERENCE_LAYERS and name not in DENSITY_SOURCES:
        raise HTTPException(status_code=404, detail="Reference layer not found")


def _require_styleable(name: str) -> None:
    if name not in REFERENCE_LAYERS:
        raise HTTPException(status_code=404, detail="Reference layer not found")


@router.get("")
async def list_reference_layers(request: Request) -> dict:
    """Name, title, zoom range, bounds and attribution of each reference layer."""
    client: httpx.AsyncClient = request.app.state.martin_client
    items = []
    for name in REFERENCE_LAYERS:
        try:
            resp = await client.get(f"/{name}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # One missing tileset should not hide the others.
            logger.warning("Reference layer %s unavailable: %s", name, exc)
            continue
        tilejson = resp.json()
        meta = LAYER_META.get(name, {})
        density = meta.get("density")
        has_density = False
        if density:
            # The heatmap tiles are built separately, so a layer may not have them yet.
            try:
                (await client.get(f"/{density}")).raise_for_status()
                has_density = True
            except httpx.HTTPError:
                pass
        items.append(
            {
                "name": name,
                "title": tilejson.get("name") or name,
                "description": tilejson.get("description"),
                "attribution": tilejson.get("attribution"),
                "minzoom": tilejson.get("minzoom", 0),
                "maxzoom": tilejson.get("maxzoom", 14),
                "bounds": tilejson.get("bounds"),
                "tile_url": f"/api/v1/reference-layers/{name}/tiles/{{z}}/{{x}}/{{y}}.pbf",
                "geometry": meta.get("geometry"),
                "attributes": meta.get("attributes", []),
                "density_tile_url": (
                    f"/api/v1/reference-layers/{density}/tiles/{{z}}/{{x}}/{{y}}.pbf" if has_density else None
                ),
            }
        )
    return {"items": items}


@router.get("/styles")
async def get_layer_styles(
    org_id: uuid.UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's organization's style overrides, by layer name."""
    rows = await db.execute(
        text("SELECT layer, style FROM reference_layer_styles WHERE organization_id = :org"),
        {"org": org_id},
    )
    return {"items": {layer: style for layer, style in rows.all()}}


@router.put("/{name}/style")
async def set_layer_style(
    name: str,
    style: LayerStyle,
    org_id: uuid.UUID = Depends(require_org_role("org:member")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Replace this organization's style for one layer. Send only the fields to override."""
    _require_styleable(name)
    try:
        check_style_for_layer(name, style)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = style.model_dump(exclude_none=True)
    await db.execute(
        text(
            "INSERT INTO reference_layer_styles (organization_id, layer, style, updated_by) "
            "VALUES (:org, :layer, CAST(:style AS jsonb), :user) "
            "ON CONFLICT (organization_id, layer) DO UPDATE "
            "SET style = EXCLUDED.style, updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"org": org_id, "layer": name, "style": json.dumps(stored), "user": user.id},
    )
    await db.commit()
    return {"layer": name, "style": stored}


@router.delete("/{name}/style", status_code=204)
async def reset_layer_style(
    name: str,
    org_id: uuid.UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Back to the default look for this organization."""
    _require_styleable(name)
    await db.execute(
        text("DELETE FROM reference_layer_styles WHERE organization_id = :org AND layer = :layer"),
        {"org": org_id, "layer": name},
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/{name}/tiles/{z}/{x}/{y}.pbf", response_class=Response)
async def get_reference_layer_tile(
    name: str,
    request: Request,
    z: int = Path(..., ge=0, le=22),
    x: int = Path(..., ge=0),
    y: int = Path(..., ge=0),
) -> Response:
    _require_layer(name)
    if x >= 2**z or y >= 2**z:
        raise HTTPException(
            status_code=422,
            detail="Tile coordinates are outside the zoom level bounds",
        )

    response = await forward_tile(
        request.app.state.martin_client,
        f"/{name}/{z}/{x}/{y}",
        default_content_type="application/x-protobuf",
    )
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return gzip_if_accepted(request, response)
