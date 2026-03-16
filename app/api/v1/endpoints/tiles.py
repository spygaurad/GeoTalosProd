"""Tile proxy endpoints.

Authenticate tile requests then stream the tile image from titiler.

All tile URLs in TileJSON responses (from ``GET /datasets/{id}/tilejson``)
are rewritten to point here, so the browser never contacts titiler directly.

Query parameters are passed through verbatim so callers can still use all
titiler rendering options (``assets``, ``colormap``, ``rescale``, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tiles", tags=["tiles"])

# Module-level httpx client with connection pooling to titiler.
_titiler_proxy_client: httpx.AsyncClient | None = None

# Content-types considered valid tile responses
_TILE_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "application/x-protobuf",  # MVT vector tiles
}


def _get_proxy_client() -> httpx.AsyncClient:
    global _titiler_proxy_client
    if _titiler_proxy_client is None or _titiler_proxy_client.is_closed:
        _titiler_proxy_client = httpx.AsyncClient(
            base_url=settings.TITILER_URL,
            timeout=30.0,
        )
    return _titiler_proxy_client


async def _proxy_tile(titiler_path: str, query_string: str) -> Response:
    """Forward a tile request to titiler and stream the response back."""
    url = titiler_path
    if query_string:
        url = f"{titiler_path}?{query_string}"

    client = _get_proxy_client()
    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.error("tile_proxy_connection_error path=%s error=%s", titiler_path, exc)
        raise HTTPException(status_code=502, detail="Tile service unavailable") from exc

    if resp.status_code == 204:
        # No tile data for this position — pass through as empty 204
        return Response(status_code=204)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Tile not found")

    if resp.status_code >= 400:
        logger.warning(
            "tile_proxy_upstream_error path=%s status=%s", titiler_path, resp.status_code
        )
        raise HTTPException(status_code=502, detail="Tile service returned an error")

    content_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()

    # Pass through useful caching headers from titiler
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
# Mosaic tiles  (collection-level, multi-item)
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

    The ``searchid`` is an opaque value returned by ``GET /datasets/{id}/tilejson``.
    All query parameters (assets, colormap, rescale, etc.) are forwarded as-is.
    """
    titiler_path = f"/mosaic/{searchid}/{z}/{x}/{y}.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))


# ---------------------------------------------------------------------------
# Single-item STAC tiles
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
    """Stream a single-item STAC tile from titiler.

    Required query parameter: ``url`` — the STAC item URL (same as used when
    calling ``GET /datasets/{id}/tilejson`` for a ``stac_item`` source type).
    """
    titiler_path = f"/stac/{z}/{x}/{y}.{fmt}"
    return await _proxy_tile(titiler_path, str(request.query_params))
