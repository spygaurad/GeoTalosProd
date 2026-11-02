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

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response

from app.services.tile_proxy import forward_tile

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
)

# Static tilesets only change when rebuilt, and the rebuild is deployed together
# with a restart, so one day is a safe browser cache lifetime.
_CACHE_CONTROL = "public, max-age=86400"


def _require_layer(name: str) -> None:
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
            }
        )
    return {"items": items}


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
    return response
