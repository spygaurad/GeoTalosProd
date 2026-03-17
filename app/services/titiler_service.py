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
        "metadata": {"collection_id": collection_id}
    }
    client = _get_client()
    resp = await client.post("/searches/register", json=payload, timeout=30.0)
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
    resp = await client.get(f"/searches/{searchid}/tilejson.json", params=params)

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
    """Replace the titiler host+path prefix with the proxy endpoint."""
    # Strip everything up to and including /mosaic/{searchid}
    # then prefix with the proxy base path.
    match = re.search(r"/searches/[^/]+(/.*)", original)
    if match:
        suffix = match.group(1)  # e.g.  /{z}/{x}/{y}.png?assets=B04
        return f"{base}/api/v1/tiles/mosaic/{searchid}{suffix}"
    # Fallback: just swap the host
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _rewrite_stac_tile_url(original: str, base: str) -> str:
    """Replace the titiler host on stac tile URLs with the proxy endpoint."""
    match = re.search(r"/stac(/.*)", original)
    if match:
        suffix = match.group(1)
        return f"{base}/api/v1/tiles/stac{suffix}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)
