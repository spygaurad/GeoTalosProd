"""TiTiler integration helpers.

All calls here are server-side (API container → titiler container).
The module-level httpx client reuses connections across requests.

Public tile URLs are rewritten to point at the main API's tile proxy
(``/api/v1/tiles/...``) so that:
  - Browsers never need a direct connection to titiler.
  - Auth is enforced on every tile request by the proxy endpoint.
  - No additional public port is required.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_titiler_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _titiler_client
    if _titiler_client is None or _titiler_client.is_closed:
        _titiler_client = httpx.AsyncClient(
            base_url=settings.TITILER_URL,
            timeout=30.0,
        )
    return _titiler_client


async def register_collection_mosaic(collection_id: str) -> str:
    """Register a pgSTAC mosaic search for a STAC collection using the collections shorthand."""
    payload = {
        "collections": [collection_id],
    }
    client = _get_client()
    try:
        resp = await client.post("/searches/register", json=payload, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError(
            "TiTiler mosaic registration timed out — service may be starting up, retry in a moment"
        )
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service — check that titiler container is healthy")
    if resp.status_code not in (200, 201):
        logger.error(
            "titiler_register_failed collection_id=%s status=%s body=%s",
            collection_id,
            resp.status_code,
            resp.text[:500],
        )
        raise RuntimeError(f"TiTiler mosaic registration failed: HTTP {resp.status_code}")
    data = resp.json()
    searchid: str = data["id"]
    logger.info("titiler_mosaic_registered collection_id=%s searchid=%s", collection_id, searchid)
    return searchid
async def get_mosaic_tilejson(
    searchid: str,
    assets: str | None = None,
    *,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a mosaic and rewrite tile URLs to the API proxy."""
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    client = _get_client()
    try:
        resp = await client.get(f"/searches/{searchid}/WebMercatorQuad/tilejson.json", params=params, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError(
            "TiTiler tilejson timed out — service may be starting up, retry in a moment"
        )
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service — check that titiler container is healthy")

    if resp.status_code == 404:
        raise RuntimeError(
            f"TiTiler search {searchid!r} returned no items for this collection. "
            "The collection may be empty or pgstac_read may lack permissions on the "
            "collection's item partition — re-run stac-db-migrate and re-ingest."
        )

    if resp.status_code != 200:
        raise RuntimeError(f"TiTiler tilejson failed: HTTP {resp.status_code}")

    tilejson: dict[str, Any] = resp.json()
    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")

    rewritten = []
    for url in tilejson.get("tiles", []):
        rewritten.append(_rewrite_mosaic_tile_url(url, searchid, base))
    tilejson["tiles"] = rewritten

    return tilejson

async def get_stac_item_tilejson(
    item_url: str,
    assets: str | None = None,
    *,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a single STAC item and rewrite tile URLs."""
    params: dict[str, Any] = {"url": item_url}
    if assets:
        params["assets"] = assets

    client = _get_client()
    resp = await client.get("/stac/tilejson.json", params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"TiTiler item tilejson failed: HTTP {resp.status_code}")

    tilejson: dict[str, Any] = resp.json()
    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")

    rewritten = []
    for url in tilejson.get("tiles", []):
        rewritten.append(_rewrite_stac_tile_url(url, base))
    tilejson["tiles"] = rewritten

    return tilejson


# ---------------------------------------------------------------------------
# URL rewriting helpers
# ---------------------------------------------------------------------------

_TITILER_HOST_RE = re.compile(r"^https?://[^/]+")


def _rewrite_mosaic_tile_url(original: str, searchid: str, base: str) -> str:
    """Rewrite a titiler-pgstac tile template URL to go through the API proxy.

    titiler-pgstac returns templates like:
      http://titiler:8000/searches/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x

    We rewrite to:
      {base}/api/v1/tiles/mosaic/{searchid}/{z}/{x}/{y}.png

    The @scale suffix (@1x) is dropped — the proxy appends it when forwarding
    to titiler.  An explicit .png extension is added so MapLibre / Leaflet
    recognise the URL as a raster tile.
    """
    # Match path segment after /tiles/{tileMatrixSetId}/ — stop before query string
    match = re.search(r"/searches/[^/]+/tiles/[^/]+/([^?]+)", original)
    if match:
        # zxy_part may be "{z}/{x}/{y}@1x" (template) or "10/512/256@1x.png" (real tile)
        zxy_part = match.group(1)
        # Strip @scale suffix (e.g. @1x, @2x) — keep any trailing .ext if present
        zxy_clean = re.sub(r"@\w+", "", zxy_part)        # "{z}/{x}/{y}" or "10/512/256.png"
        # Ensure .png extension
        if not re.search(r"\.\w+$", zxy_clean):
            zxy_clean = f"{zxy_clean}.png"
        # Preserve original query string
        qs_match = re.search(r"\?(.+)$", original)
        qs = f"?{qs_match.group(1)}" if qs_match else ""
        return f"{base}/api/v1/tiles/mosaic/{searchid}/{zxy_clean}{qs}"
    # Fallback: just swap the host
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _rewrite_stac_tile_url(original: str, base: str) -> str:
    """Replace the titiler host on stac tile URLs with the proxy endpoint."""
    match = re.search(r"/stac(/.*)", original)
    if match:
        suffix = match.group(1)
        return f"{base}/api/v1/tiles/stac{suffix}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)
