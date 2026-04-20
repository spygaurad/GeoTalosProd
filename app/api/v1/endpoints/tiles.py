"""Tile proxy endpoints — three-tier architecture.

Authenticate tile requests then stream the tile image from titiler.

All tile URLs in TileJSON responses are rewritten to point here, so the
browser never contacts titiler directly.

Proxy tiers:
  /tiles/collections/{cid}/{z}/{x}/{y}.{fmt}             — whole collection
  /tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.{fmt}  — single item (org-verified)
  /tiles/mosaic/{searchid}/{z}/{x}/{y}.{fmt}               — multi-collection / multi-item search
  /tiles/raster-masks/{set_id}/{z}/{x}/{y}.{fmt}           — segmentation mask with server-side colormap

Query parameters are passed through verbatim so callers can still use all
titiler rendering options (``assets``, ``colormap``, ``rescale``, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.core.enums import DatasetStatus
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.user import User
from app.schemas.tile import MultiDatasetTileRequest, MultiItemTileRequest
from app.services import titiler_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tiles", tags=["tiles"])

# Module-level httpx client with connection pooling to titiler.
_titiler_proxy_client: httpx.AsyncClient | None = None


def _get_proxy_client() -> httpx.AsyncClient:
    global _titiler_proxy_client
    if _titiler_proxy_client is None or _titiler_proxy_client.is_closed:
        _titiler_proxy_client = httpx.AsyncClient(
            base_url=settings.TITILER_URL,
            timeout=30.0,
        )
    return _titiler_proxy_client


async def _proxy_tile(titiler_path: str, query_string: str) -> Response:
    """Forward a tile request to titiler and stream the response back.
    
    Automatically adds `assets=data` if no assets/expression is specified,
    as titiler-pgstac requires this parameter for STAC item tiles.
    """
    # Ensure assets parameter is present (titiler requires it)
    if query_string:
        # Check if assets or expression is already specified
        qs_lower = query_string.lower()
        if "assets=" not in qs_lower and "expression=" not in qs_lower:
            query_string = f"{query_string}&assets=data"
        url = f"{titiler_path}?{query_string}"
    else:
        # No query string - add default assets
        url = f"{titiler_path}?assets=data"

    client = _get_proxy_client()
    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.error("tile_proxy_connection_error path=%s error=%s", titiler_path, exc)
        raise HTTPException(status_code=502, detail="Tile service unavailable") from exc

    if resp.status_code == 204:
        return Response(status_code=204)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Tile not found")

    if resp.status_code >= 400:
        body_preview = resp.text[:500] if resp.text else "(empty)"
        logger.warning(
            "tile_proxy_upstream_error path=%s status=%s body=%s",
            titiler_path, resp.status_code, body_preview,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Tile service error (HTTP {resp.status_code}): {body_preview}",
        )

    content_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()

    headers: dict[str, str] = {}
    for header in ("cache-control", "etag", "last-modified", "content-length"):
        if header in resp.headers:
            headers[header] = resp.headers[header]

    return Response(
        content=resp.content,
        status_code=200,
        media_type=content_type,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Tier 1: Collection tiles (whole dataset)
# ---------------------------------------------------------------------------

@router.get("/collections/{collection_id}/{z}/{x}/{y}.{fmt}")
async def proxy_collection_tile(
    collection_id: str,
    z: int,
    x: int,
    y: int,
    fmt: str,
    request: Request,
    _org_id: Any = Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
    _db: Any = Depends(get_session),
) -> Response:
    """Stream a collection-level tile from titiler."""
    titiler_path = f"/collections/{collection_id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))


# ---------------------------------------------------------------------------
# Tier 2: Item tiles (single dataset item)
# ---------------------------------------------------------------------------

@router.get("/collections/{collection_id}/items/{item_id}/{z}/{x}/{y}.{fmt}")
async def proxy_item_tile(
    collection_id: str,
    item_id: str,
    z: int,
    x: int,
    y: int,
    fmt: str,
    request: Request,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a single-item tile from titiler.

    Verifies that the calling org owns the dataset item before proxying to
    titiler — prevents cross-org leakage by guessing STAC item IDs.
    """
    result = await db.execute(
        select(DatasetItem).where(
            DatasetItem.stac_item_id == item_id,
            DatasetItem.stac_collection_id == collection_id,
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tile not found")

    titiler_path = f"/collections/{collection_id}/items/{item_id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))


# ---------------------------------------------------------------------------
# Tier 3: Mosaic tiles (search-based — multi-collection / multi-item)
# ---------------------------------------------------------------------------

@router.get("/raster-masks/{annotation_set_id}/{z}/{x}/{y}.{fmt}")
async def proxy_raster_mask_tile(
    annotation_set_id: UUID,
    z: int,
    x: int,
    y: int,
    fmt: str,
    request: Request,
) -> Response:
    """Stream a colored segmentation-mask tile with server-side colormap applied.

    This endpoint is exempt from ClerkAuth (see clerk_auth.py) because map
    libraries load tile URLs as image <src> and cannot attach Authorization
    headers.  The annotation_set UUID is the capability token.

    Raster config is fetched via a SECURITY DEFINER function that bypasses RLS
    for this single read-only, non-sensitive field (rendering metadata only —
    no org/user/geometry data is exposed).
    """
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    async with AsyncSessionLocal() as session:
        row = await session.execute(
            sa_text("SELECT get_raster_config_public(:id)"),
            {"id": str(annotation_set_id)},
        )
        raster_cfg = row.scalar_one_or_none()

    if raster_cfg is None:
        raise HTTPException(
            status_code=404,
            detail="Raster mask not found or not accessible",
        )

    if not raster_cfg:
        raise HTTPException(
            status_code=400,
            detail="Annotation set has no raster mask config; call PATCH /annotation-sets/{id}/raster/config first",
        )

    colormap = raster_cfg.get("colormap")
    if not colormap:
        raise HTTPException(
            status_code=400,
            detail="Raster mask config has no persisted colormap; re-save the raster config to generate one",
        )

    stac_collection_id = raster_cfg["stac_collection_id"]
    stac_item_id = raster_cfg["stac_item_id"]
    band_index = raster_cfg.get("band_index", 1)

    titiler_path = (
        f"/collections/{stac_collection_id}/items/{stac_item_id}"
        f"/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.{fmt}"
    )

    # Build query string with colormap applied server-side.
    # URL-encode the colormap JSON so special characters are safe in the URL.
    colormap_encoded = quote(json.dumps(colormap), safe="")
    qs = f"assets=data&asset_bidx=data|{band_index}&colormap={colormap_encoded}"

    # Pass through any extra query params from the request (e.g. rescale, opacity).
    req_qs = str(request.query_params)
    if req_qs:
        qs = f"{qs}&{req_qs}"

    return await _proxy_tile(titiler_path, qs)


# ---------------------------------------------------------------------------
# Tier 3: Mosaic tiles (search-based — multi-collection / multi-item)
# ---------------------------------------------------------------------------

@router.get("/mosaic/{searchid}/{z}/{x}/{y}.{fmt}")
async def proxy_mosaic_tile(
    searchid: str,
    z: int,
    x: int,
    y: int,
    fmt: str,
    request: Request,
    _org_id: Any = Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
    _db: Any = Depends(get_session),
) -> Response:
    """Stream a mosaic tile from titiler.

    The ``searchid`` is an opaque value returned by the tilejson endpoints.
    All query parameters (assets, colormap, rescale, etc.) are forwarded as-is.
    """
    titiler_path = f"/searches/{searchid}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))


# ---------------------------------------------------------------------------
# TileJSON endpoints for multi-dataset and multi-item mosaics
# ---------------------------------------------------------------------------

@router.post("/mosaic/multi-dataset/tilejson")
async def get_multi_dataset_tilejson(
    payload: MultiDatasetTileRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Register and return TileJSON for a mosaic spanning multiple datasets.

    Accepts a list of dataset IDs; all must belong to the caller's org and
    be in ``ready`` status.  Returns a composite TileJSON with a ``searchid``
    that can be used with ``GET /tiles/mosaic/{searchid}/{z}/{x}/{y}.{fmt}``.

    For uint16 satellite data, pass ``rescale`` (e.g., "0,10000") to normalize
    values for PNG rendering. For 4+ band data, pass ``asset_bidx`` (e.g.,
    "data|1,2,3") to select specific bands.
    """
    result = await db.execute(
        select(Dataset).where(
            Dataset.id.in_(payload.dataset_ids),
            Dataset.organization_id == org_id,
            Dataset.deleted_at.is_(None),
        )
    )
    datasets = result.scalars().all()

    if len(datasets) != len(payload.dataset_ids):
        raise HTTPException(status_code=404, detail="One or more datasets not found")

    not_ready = [str(ds.id) for ds in datasets if ds.status != DatasetStatus.READY]
    if not_ready:
        raise HTTPException(
            status_code=409,
            detail=f"Datasets not ready for tiling: {', '.join(not_ready)}",
        )

    collection_ids = [ds.stac_collection_id for ds in datasets if ds.stac_collection_id]
    if not collection_ids:
        raise HTTPException(status_code=409, detail="No STAC collections registered for these datasets")

    # Use rendering_config from the first dataset as mosaic default
    rendering_config = None
    for ds in datasets:
        rc = (ds.metadata_ or {}).get("rendering_config")
        if rc:
            rendering_config = rc
            break

    try:
        searchid = await titiler_service.register_multi_collection_mosaic(collection_ids)
        tilejson = await titiler_service.get_mosaic_tilejson(
            searchid,
            assets=payload.assets,
            rendering_config=rendering_config,
            preset=payload.preset,
            rescale=payload.rescale,
            asset_bidx=payload.asset_bidx,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Tile service error: {exc}") from exc

    return {
        **tilejson,
        "searchid": searchid,
        "dataset_ids": [str(did) for did in payload.dataset_ids],
    }


@router.post("/mosaic/multi-item/tilejson")
async def get_multi_item_tilejson(
    payload: MultiItemTileRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Register and return TileJSON for a mosaic of specific dataset items.

    Items may come from different datasets/collections.  Returns a composite
    TileJSON with a ``searchid`` for use with the mosaic tile proxy.

    For uint16 satellite data, pass ``rescale`` (e.g., "0,10000") to normalize
    values for PNG rendering. For 4+ band data, pass ``asset_bidx`` (e.g.,
    "data|1,2,3") to select specific bands.
    """
    result = await db.execute(
        select(DatasetItem).where(
            DatasetItem.stac_item_id.in_(payload.item_ids),
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    items = result.scalars().all()

    if not items:
        raise HTTPException(status_code=404, detail="No matching dataset items found")

    # Deduplicate by stac_item_id (same item may appear in multiple collections)
    seen: set[str] = set()
    unique_items = []
    for item in items:
        if item.stac_item_id not in seen:
            seen.add(item.stac_item_id)
            unique_items.append(item)
    items = unique_items

    stac_item_ids = [item.stac_item_id for item in items]
    collection_ids = list({item.stac_collection_id for item in items})

    # Use rendering_config from the first item as mosaic default
    rendering_config = None
    for item in items:
        rc = (item.properties_cache or {}).get("rendering_config")
        if rc:
            rendering_config = rc
            break

    try:
        searchid = await titiler_service.register_item_mosaic(stac_item_ids, collection_ids)
        tilejson = await titiler_service.get_mosaic_tilejson(
            searchid,
            assets=payload.assets,
            rendering_config=rendering_config,
            preset=payload.preset,
            rescale=payload.rescale,
            asset_bidx=payload.asset_bidx,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Tile service error: {exc}") from exc

    return {
        **tilejson,
        "searchid": searchid,
        "item_ids": [str(iid) for iid in payload.item_ids],
    }


# ---------------------------------------------------------------------------
# Legacy: STAC tile proxy (kept for backward compatibility)
# ---------------------------------------------------------------------------

@router.get("/stac/{z}/{x}/{y}.{fmt}")
async def proxy_stac_tile(
    z: int,
    x: int,
    y: int,
    fmt: str,
    request: Request,
    _org_id: Any = Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
    _db: Any = Depends(get_session),
) -> Response:
    """Stream a single-item STAC tile from titiler (legacy endpoint).

    Prefer ``/tiles/collections/{cid}/items/{iid}/...`` instead.
    """
    titiler_path = f"/stac/{z}/{x}/{y}.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))
