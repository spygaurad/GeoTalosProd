"""TiTiler integration helpers — three-tier tile serving.

All calls here are server-side (API container -> titiler container).
The module-level httpx client reuses connections across requests.

Public tile URLs are rewritten to point at the main API's tile proxy
(``/api/v1/tiles/...``) so that:
  - Browsers never need a direct connection to titiler.
  - Auth is enforced on every tile request by the proxy endpoint.
  - No additional public port is required.

Tier overview (titiler-pgstac 2.1.0):
  - Collections:  /collections/{cid}/...         — whole dataset mosaic
  - Items:        /collections/{cid}/items/{iid}/ — single item tiles
  - Searches:     /searches/{sid}/...             — multi-collection / filtered mosaics

Performance notes for choosing the right endpoint:
  - **Single STAC Item**: Use Tier 2 (Items endpoint) — direct, no search registration.
  - **Whole Collection**: Use Tier 1 (Collections endpoint) — direct, uses pgSTAC index.
  - **Multiple specific items**: Use Tier 3 (Searches) with `ids` filter — requires
    one-time registration but the search ID is cached by pgSTAC (idempotent hash).
  - **Filtered collection** (by datetime/bbox): Use Collections endpoint with query
    params — no registration needed, filter applied per-request.

Default pgSTAC optimizations (can be tuned via query params):
  - exitwhenfull=true: Stop scanning once tile area is fully covered.
  - skipcovered=true: Skip items fully covered by previous items.
  - items_limit=100, scan_limit=10000, time_limit=5: Resource guards.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TITILER_HOST_RE = re.compile(r"^https?://[^/]+")

# Default rescale ranges for common data types
# uint16: 0-65535 -> 0-255 (common for satellite imagery)
# uint8: no rescaling needed
DEFAULT_RESCALE_UINT16 = "0,10000"  # Conservative range for most satellite data


async def _titiler_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET request to titiler with standard error handling."""
    client = _get_client()
    try:
        resp = await client.get(path, params=params, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError("TiTiler request timed out — service may be starting up, retry in a moment")
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service — check that titiler container is healthy")

    if resp.status_code == 404:
        raise RuntimeError(f"TiTiler returned 404 for {path} — resource may not exist or collection may be empty")
    if resp.status_code != 200:
        logger.error("titiler_request_failed path=%s status=%s body=%s", path, resp.status_code, resp.text[:500])
        raise RuntimeError(f"TiTiler request failed: HTTP {resp.status_code}")

    return resp.json()


async def _get_item_rendering_params(
    collection_id: str,
    item_id: str,
    asset_name: str = "data",
) -> dict[str, str]:
    """Detect data type and band count, return rendering params for PNG compatibility.

    Uses TiTiler's standard parameters:
    - rescale: min,max value range for uint16 → uint8 conversion
    - expression: band selection (e.g., "b1" for first band)
    - colormap_name: viridis/gray for single-band visualization
    - nodata: explicit NoData value masking

    Auto-detection rules:
    - Single-band grayscale: expression=b1, colormap=gray, rescale from stats
    - 3-band RGB (ColorInterp=Red/Green/Blue): no params needed
    - 3-band non-RGB (Gray/Undefined): expression=b1, colormap=viridis, rescale
    - 4+ bands: expression=b1;b2;b3 (first 3 as RGB)
    - uint16 with NoData=65535: add nodata=65535

    Returns dict like {"rescale": "0,5000", "expression": "b1", "colormap_name": "viridis"}
    """
    try:
        info = await _titiler_get(
            f"/collections/{collection_id}/items/{item_id}/info",
            params={"assets": asset_name},
        )
    except RuntimeError:
        logger.debug("Could not get item info for rendering params")
        return {}

    params: dict[str, str] = {}
    asset_info = info.get(asset_name, {})
    dtype = asset_info.get("dtype", "uint8")
    band_metadata = asset_info.get("band_metadata", [])
    band_count = len(band_metadata)
    nodata = asset_info.get("nodata_value")

    # Read colorinterp from top-level array (titiler puts it there, NOT in band_metadata)
    color_interps: list[str] = [
        ci.lower() for ci in asset_info.get("colorinterp", [])
    ]

    # Determine band layout
    is_rgb = color_interps[:3] == ["red", "green", "blue"]
    is_rgba = color_interps[:4] == ["red", "green", "blue", "alpha"]
    is_grayscale = len(color_interps) > 0 and color_interps[0] == "gray"

    logger.debug(
        "Auto-detect for %s/%s: dtype=%s bands=%d colorinterp=%s is_rgb=%s is_gray=%s",
        collection_id, item_id, dtype, band_count, color_interps, is_rgb, is_grayscale,
    )

    # Handle uint16/int16 data — needs rescaling for PNG output
    if dtype in ("uint16", "int16"):
        try:
            stats = await _titiler_get(
                f"/collections/{collection_id}/items/{item_id}/statistics",
                params={"assets": asset_name},
            )
            # titiler returns stats as flat keys: "{asset}_b{n}" e.g. "data_b1", "data_b2"
            # OR nested: "{asset}": {"1": {...}} — handle both formats
            if is_rgb or is_rgba:
                render_bands = [1, 2, 3]
            else:
                render_bands = [1]
            all_min, all_max = float("inf"), float("-inf")
            for bnum in render_bands:
                # Try flat key first: "data_b1"
                bs = stats.get(f"{asset_name}_b{bnum}", {})
                if not bs:
                    # Try nested: stats["data"]["1"]
                    bs = stats.get(asset_name, {}).get(str(bnum), {})
                bmin = bs.get("percentile_2", bs.get("min", 0))
                bmax = bs.get("percentile_98", bs.get("max", 10000))
                if bmin < all_min:
                    all_min = bmin
                if bmax > all_max:
                    all_max = bmax
            if all_min < all_max:
                params["rescale"] = f"{int(all_min)},{int(all_max)}"
            else:
                params["rescale"] = DEFAULT_RESCALE_UINT16
            logger.debug("Auto-rescale from stats for %s: %s", item_id, params["rescale"])
        except Exception as exc:
            params["rescale"] = DEFAULT_RESCALE_UINT16
            logger.debug("Auto-rescale (default) for %s: %s (error: %s)", item_id, params["rescale"], exc)

        # Add nodata masking if present
        if nodata is not None:
            params["nodata"] = str(nodata)

    # Handle band selection based on colorinterp
    if is_rgb or is_rgba:
        # True RGB(A) — select bands 1,2,3 explicitly (skip alpha if present)
        params["asset_bidx"] = f"{asset_name}|1,2,3"
        logger.debug("Auto-rendering RGB for %s: asset_bidx=%s", item_id, params["asset_bidx"])
    elif is_grayscale or band_count == 1:
        # Grayscale — render band 1 with gray colormap
        params["asset_bidx"] = f"{asset_name}|1"
        params["colormap_name"] = "gray"
        logger.debug("Auto-rendering grayscale for %s: asset_bidx=%s", item_id, params["asset_bidx"])
    elif band_count == 3:
        # 3 bands but no RGB colorinterp — assume RGB anyway (common for drone orthomosaics)
        params["asset_bidx"] = f"{asset_name}|1,2,3"
        logger.debug("Auto-rendering assumed-RGB for %s (3 bands, no colorinterp)", item_id)
    elif band_count >= 4:
        # 4+ bands with no RGB colorinterp — render band 1 as grayscale
        params["asset_bidx"] = f"{asset_name}|1"
        params["colormap_name"] = "gray"
        logger.debug("Auto-rendering band-1 grayscale for multi-band %s (bands=%d)", item_id, band_count)

    return params


async def _get_collection_rendering_params(
    collection_id: str,
    asset_name: str = "data",
) -> dict[str, str]:
    """Detect data type from collection's first item and return rendering params.

    Queries the STAC API for a sample item from the collection, then uses
    titiler's item info endpoint to detect dtype and band count.
    """
    # Get a sample item from the STAC API
    try:
        stac_client = httpx.AsyncClient(
            base_url=settings.STAC_API_URL,
            timeout=15.0,
        )
        try:
            resp = await stac_client.get(
                f"/collections/{collection_id}/items",
                params={"limit": 1},
            )
            if resp.status_code != 200:
                logger.debug("STAC API items query failed for %s: %s", collection_id, resp.status_code)
                return {}
            features = resp.json().get("features", [])
            if not features:
                return {}
            sample_item_id = features[0]["id"]
        finally:
            await stac_client.aclose()
    except Exception:
        logger.debug("Could not fetch sample item from STAC API for %s", collection_id)
        return {}

    return await _get_item_rendering_params(collection_id, sample_item_id, asset_name)


async def _register_search(payload: dict[str, Any]) -> str:
    """Register a pgSTAC search and return the search ID.

    titiler-pgstac hashes the search payload to produce the ID, so identical
    payloads always return the same search ID (idempotent).
    """
    client = _get_client()
    try:
        resp = await client.post("/searches/register", json=payload, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError("TiTiler search registration timed out — service may be starting up")
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service — check that titiler container is healthy")

    if resp.status_code not in (200, 201):
        logger.error("titiler_register_failed payload=%s status=%s body=%s", payload, resp.status_code, resp.text[:500])
        raise RuntimeError(f"TiTiler search registration failed: HTTP {resp.status_code}")

    data = resp.json()
    searchid: str = data["id"]
    logger.info("titiler_search_registered searchid=%s payload_keys=%s", searchid, list(payload.keys()))
    return searchid


# ---------------------------------------------------------------------------
# Pre-computed rendering config → titiler params
# ---------------------------------------------------------------------------

def _rendering_params_from_config(
    rendering_config: dict | None,
    preset: str | None = None,
) -> dict[str, str] | None:
    """Convert a stored rendering_config to titiler query params.

    Returns None if the config is missing or malformed, signalling the caller
    to fall back to the on-the-fly titiler auto-detection.
    """
    if not rendering_config or not isinstance(rendering_config, dict):
        return None

    presets = rendering_config.get("presets")
    if not presets or not isinstance(presets, dict):
        return None

    # Pick the requested preset, or the default
    preset_id = preset or rendering_config.get("default_preset")
    if not preset_id or preset_id not in presets:
        # Try the first preset as ultimate fallback
        preset_id = next(iter(presets), None)
    if not preset_id:
        return None

    preset_data = presets[preset_id]
    params = preset_data.get("params")
    if not params or not isinstance(params, dict):
        return None

    # Return a copy with string values
    return {k: str(v) for k, v in params.items()}


def _inject_rendering_info(
    tilejson: dict[str, Any],
    rendering_config: dict | None,
    current_preset: str | None,
) -> None:
    """Add rendering metadata to a tilejson response (mutates in place)."""
    if not rendering_config or not isinstance(rendering_config, dict):
        return
    tilejson["rendering"] = {
        "data_category": rendering_config.get("data_category"),
        "current_preset": current_preset or rendering_config.get("default_preset"),
        "available_presets": {
            pid: {"label": p.get("label", pid), "params": p.get("params", {})}
            for pid, p in rendering_config.get("presets", {}).items()
        },
    }


# ---------------------------------------------------------------------------
# Tier 1: Collections — whole dataset mosaic (no search registration)
# ---------------------------------------------------------------------------

async def get_collection_tilejson(
    collection_id: str,
    assets: str | None = None,
    *,
    rendering_config: dict | None = None,
    preset: str | None = None,
    rescale: str | None = None,
    asset_bidx: str | None = None,
    expression: str | None = None,
    colormap_name: str | None = None,
    nodata: str | None = None,
    auto_rescale: bool = True,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a whole STAC collection and rewrite tile URLs.

    Uses titiler's ``/collections/{cid}/WebMercatorQuad/tilejson.json``.

    When ``rendering_config`` is provided (pre-computed during ingestion),
    rendering params are read from the config with zero titiler HTTP calls.
    Falls back to on-the-fly detection for older items without cached config.

    Args:
        collection_id: STAC collection ID
        assets: Comma-separated asset names (default: "data")
        rendering_config: Pre-computed rendering metadata (from DB)
        preset: Rendering preset name (e.g., "natural_color", "ndvi")
        rescale: Explicit rescale range (e.g., "0,5000")
        expression: Band expression (e.g., "b1" or "b1;b2;b3")
        colormap_name: Colormap for single-band (e.g., "viridis", "gray")
        nodata: NoData value for masking (e.g., "65535")
        auto_rescale: Auto-detect uint16 data and add rescale/expression params (default: True)
        public_api_url: Override the public API base URL for tile rewrites

    Returns:
        TileJSON dict with rewritten tile URLs + rendering metadata
    """
    asset_name = (assets or "data").split(",")[0]

    # Build request params
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    # Resolve rendering params: cached config → titiler auto-detect → explicit overrides
    rendering_params: dict[str, str] = {}
    if auto_rescale and not rescale:
        cached = _rendering_params_from_config(rendering_config, preset)
        if cached is not None:
            rendering_params = cached
            logger.debug("Using cached rendering_config for %s (preset=%s)", collection_id, preset)
        else:
            rendering_params = await _get_collection_rendering_params(collection_id, asset_name)

    # Explicit params override auto-detected / cached
    if rescale:
        rendering_params["rescale"] = rescale
    if asset_bidx:
        rendering_params["asset_bidx"] = asset_bidx
    if expression:
        rendering_params["expression"] = expression
    if colormap_name:
        rendering_params["colormap_name"] = colormap_name
    if nodata:
        rendering_params["nodata"] = nodata

    params.update(rendering_params)

    tilejson = await _titiler_get(
        f"/collections/{collection_id}/WebMercatorQuad/tilejson.json",
        params=params,
    )

    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")
    tilejson["tiles"] = [
        _rewrite_collection_tile_url(url, collection_id, base)
        for url in tilejson.get("tiles", [])
    ]

    _inject_rendering_info(tilejson, rendering_config, preset)
    return tilejson


# ---------------------------------------------------------------------------
# Tier 2: Items — single item tiles
# ---------------------------------------------------------------------------

async def get_item_tilejson(
    collection_id: str,
    item_id: str,
    assets: str | None = None,
    *,
    rendering_config: dict | None = None,
    preset: str | None = None,
    rescale: str | None = None,
    asset_bidx: str | None = None,
    auto_rescale: bool = True,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a single STAC item and rewrite tile URLs.

    When ``rendering_config`` is provided, rendering params are read from the
    config with zero titiler HTTP calls. Falls back to on-the-fly detection.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID
        assets: Comma-separated asset names (default: "data")
        rendering_config: Pre-computed rendering metadata (from DB)
        preset: Rendering preset name (e.g., "natural_color", "ndvi")
        rescale: Explicit rescale range (e.g., "0,10000")
        asset_bidx: Explicit band selection (e.g., "data|1,2,3")
        auto_rescale: Auto-detect uint16 data and add rescale/bidx params (default: True)
        public_api_url: Override the public API base URL for tile rewrites

    Returns:
        TileJSON dict with rewritten tile URLs + rendering metadata
    """
    asset_name = (assets or "data").split(",")[0]

    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    rendering_params: dict[str, str] = {}
    if auto_rescale and not rescale:
        cached = _rendering_params_from_config(rendering_config, preset)
        if cached is not None:
            rendering_params = cached
            logger.debug("Using cached rendering_config for %s/%s (preset=%s)", collection_id, item_id, preset)
        else:
            rendering_params = await _get_item_rendering_params(collection_id, item_id, asset_name)

    if rescale:
        rendering_params["rescale"] = rescale
    if asset_bidx:
        rendering_params["asset_bidx"] = asset_bidx

    params.update(rendering_params)

    tilejson = await _titiler_get(
        f"/collections/{collection_id}/items/{item_id}/tilejson.json",
        params=params,
    )

    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")
    tilejson["tiles"] = [
        _rewrite_item_tile_url(url, collection_id, item_id, base)
        for url in tilejson.get("tiles", [])
    ]

    _inject_rendering_info(tilejson, rendering_config, preset)
    return tilejson


# ---------------------------------------------------------------------------
# Tier 3: Searches — multi-collection / multi-item / filtered mosaics
# ---------------------------------------------------------------------------

async def register_collection_mosaic(collection_id: str) -> str:
    """Register a pgSTAC mosaic search for a single STAC collection."""
    return await _register_search({"collections": [collection_id]})


async def register_multi_collection_mosaic(collection_ids: list[str]) -> str:
    """Register a pgSTAC mosaic search spanning multiple STAC collections."""
    return await _register_search({"collections": collection_ids})


async def register_item_mosaic(
    item_ids: list[str],
    collection_ids: list[str] | None = None,
) -> str:
    """Register a pgSTAC search for specific STAC items."""
    payload: dict[str, Any] = {"ids": item_ids}
    if collection_ids:
        payload["collections"] = collection_ids
    return await _register_search(payload)


async def get_mosaic_tilejson(
    searchid: str,
    assets: str | None = None,
    *,
    rendering_config: dict | None = None,
    preset: str | None = None,
    rescale: str | None = None,
    asset_bidx: str | None = None,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a mosaic search and rewrite tile URLs to the API proxy.

    When ``rendering_config`` is provided, default rendering params are used
    from the config. Explicit ``rescale``/``asset_bidx`` still override.

    Args:
        searchid: pgSTAC search hash ID
        assets: Comma-separated asset names
        rendering_config: Pre-computed rendering metadata (from DB)
        preset: Rendering preset name
        rescale: Rescale range (e.g., "0,10000") for uint16 data
        asset_bidx: Band selection (e.g., "data|1,2,3")
        public_api_url: Override the public API base URL

    Returns:
        TileJSON dict with rewritten tile URLs + rendering metadata
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    # Use cached config if available (mosaic previously had no auto-detection)
    rendering_params: dict[str, str] = {}
    if not rescale and not asset_bidx:
        cached = _rendering_params_from_config(rendering_config, preset)
        if cached is not None:
            rendering_params = cached

    if rescale:
        rendering_params["rescale"] = rescale
    if asset_bidx:
        rendering_params["asset_bidx"] = asset_bidx

    params.update(rendering_params)

    tilejson = await _titiler_get(
        f"/searches/{searchid}/WebMercatorQuad/tilejson.json",
        params=params,
    )

    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")
    tilejson["tiles"] = [
        _rewrite_mosaic_tile_url(url, searchid, base)
        for url in tilejson.get("tiles", [])
    ]

    _inject_rendering_info(tilejson, rendering_config, preset)
    return tilejson


# ---------------------------------------------------------------------------
# Legacy (kept for backward compatibility — prefer collection/item tiers)
# ---------------------------------------------------------------------------

async def get_stac_item_tilejson(
    item_url: str,
    assets: str | None = None,
    *,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a single STAC item via the legacy /stac/ endpoint."""
    params: dict[str, Any] = {"url": item_url}
    if assets:
        params["assets"] = assets

    tilejson = await _titiler_get("/stac/tilejson.json", params=params)

    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")
    tilejson["tiles"] = [
        _rewrite_stac_tile_url(url, base)
        for url in tilejson.get("tiles", [])
    ]
    return tilejson


# ---------------------------------------------------------------------------
# URL rewriting helpers
# ---------------------------------------------------------------------------

def _rewrite_collection_tile_url(original: str, collection_id: str, base: str) -> str:
    """Rewrite a titiler collection tile URL to the API proxy.

    titiler returns:
      http://titiler:8000/collections/{cid}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x
    We rewrite to:
      {base}/api/v1/tiles/collections/{cid}/{z}/{x}/{y}.png
    """
    match = re.search(r"/collections/[^/]+/tiles/[^/]+/([^?]+)", original)
    if match:
        zxy_part = _clean_zxy(match.group(1))
        qs = _extract_qs(original)
        return f"{base}/api/v1/tiles/collections/{collection_id}/{zxy_part}{qs}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _rewrite_item_tile_url(original: str, collection_id: str, item_id: str, base: str) -> str:
    """Rewrite a titiler item tile URL to the API proxy.

    titiler returns:
      http://titiler:8000/collections/{cid}/items/{iid}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x
    We rewrite to:
      {base}/api/v1/tiles/collections/{cid}/items/{iid}/{z}/{x}/{y}.png
    """
    match = re.search(r"/collections/[^/]+/items/[^/]+/tiles/[^/]+/([^?]+)", original)
    if match:
        zxy_part = _clean_zxy(match.group(1))
        qs = _extract_qs(original)
        return f"{base}/api/v1/tiles/collections/{collection_id}/items/{item_id}/{zxy_part}{qs}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _rewrite_mosaic_tile_url(original: str, searchid: str, base: str) -> str:
    """Rewrite a titiler-pgstac mosaic tile URL to the API proxy.

    titiler-pgstac returns:
      http://titiler:8000/searches/{id}/tiles/WebMercatorQuad/{z}/{x}/{y}@1x
    We rewrite to:
      {base}/api/v1/tiles/mosaic/{searchid}/{z}/{x}/{y}.png
    """
    match = re.search(r"/searches/[^/]+/tiles/[^/]+/([^?]+)", original)
    if match:
        zxy_part = _clean_zxy(match.group(1))
        qs = _extract_qs(original)
        return f"{base}/api/v1/tiles/mosaic/{searchid}/{zxy_part}{qs}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _rewrite_stac_tile_url(original: str, base: str) -> str:
    """Replace the titiler host on legacy stac tile URLs with the proxy endpoint."""
    match = re.search(r"/stac(/.*)", original)
    if match:
        suffix = match.group(1)
        return f"{base}/api/v1/tiles/stac{suffix}"
    return _TITILER_HOST_RE.sub(f"{base}/api/v1/tiles", original)


def _clean_zxy(zxy_part: str) -> str:
    """Strip @scale suffix and ensure .png extension.

    Transforms e.g. ``{z}/{x}/{y}@1x`` → ``{z}/{x}/{y}.png``.
    """
    zxy_clean = re.sub(r"@\w+", "", zxy_part)
    if not re.search(r"\.\w+$", zxy_clean):
        zxy_clean = f"{zxy_clean}.png"
    return zxy_clean


def _extract_qs(url: str) -> str:
    """Extract query string from a URL, including the leading ``?``."""
    qs_match = re.search(r"\?(.+)$", url)
    return f"?{qs_match.group(1)}" if qs_match else ""


# ---------------------------------------------------------------------------
# Enhanced Streaming: Filtered Collections
# ---------------------------------------------------------------------------

async def get_filtered_collection_tilejson(
    collection_id: str,
    assets: str | None = None,
    *,
    item_ids: list[str] | None = None,
    datetime_range: str | None = None,
    bbox: list[float] | None = None,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Fetch TileJSON for a filtered subset of a STAC collection.

    This is more efficient than registering a search when you only need
    basic filtering. titiler-pgstac applies the filters per-request.

    Args:
        collection_id: STAC collection ID
        assets: Comma-separated asset names (e.g., "data" or "B01,B02,B03")
        item_ids: Filter to specific STAC item IDs within the collection
        datetime_range: ISO 8601 datetime or interval (e.g., "2024-01-01/2024-12-31")
        bbox: Bounding box [west, south, east, north] in EPSG:4326
        public_api_url: Override the public API base URL for tile rewrites

    Returns:
        TileJSON dict with rewritten tile URLs
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets
    if item_ids:
        params["ids"] = ",".join(item_ids)
    if datetime_range:
        params["datetime"] = datetime_range
    if bbox:
        params["bbox"] = ",".join(str(v) for v in bbox)

    tilejson = await _titiler_get(
        f"/collections/{collection_id}/WebMercatorQuad/tilejson.json",
        params=params,
    )

    base = (public_api_url or settings.PUBLIC_API_URL).rstrip("/")
    # Append filter params to rewritten URLs so the proxy can forward them
    filter_qs = _build_filter_qs(item_ids=item_ids, datetime_range=datetime_range, bbox=bbox)
    tilejson["tiles"] = [
        _rewrite_collection_tile_url(url, collection_id, base) + filter_qs
        for url in tilejson.get("tiles", [])
    ]
    return tilejson


def _build_filter_qs(
    item_ids: list[str] | None = None,
    datetime_range: str | None = None,
    bbox: list[float] | None = None,
) -> str:
    """Build query string for filter parameters."""
    params: dict[str, str] = {}
    if item_ids:
        params["ids"] = ",".join(item_ids)
    if datetime_range:
        params["datetime"] = datetime_range
    if bbox:
        params["bbox"] = ",".join(str(v) for v in bbox)
    if not params:
        return ""
    return "&" + urlencode(params)


# ---------------------------------------------------------------------------
# Enhanced Streaming: Preview Images
# ---------------------------------------------------------------------------

async def get_item_preview(
    collection_id: str,
    item_id: str,
    assets: str | None = None,
    *,
    max_size: int = 1024,
    format: str = "png",
    rescale: str | None = None,
    colormap_name: str | None = None,
) -> bytes:
    """Fetch a preview image for a single STAC item.

    Returns the raw image bytes. Useful for thumbnails and quick previews.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID
        assets: Comma-separated asset names
        max_size: Maximum dimension in pixels (default 1024)
        format: Image format ("png", "jpeg", "webp")
        rescale: Min,max rescale range (e.g., "0,255")
        colormap_name: Named colormap (e.g., "viridis", "cfastie")

    Returns:
        Raw image bytes
    """
    params: dict[str, Any] = {"max_size": max_size}
    if assets:
        params["assets"] = assets
    if rescale:
        params["rescale"] = rescale
    if colormap_name:
        params["colormap_name"] = colormap_name

    client = _get_client()
    path = f"/collections/{collection_id}/items/{item_id}/preview.{format}"

    try:
        resp = await client.get(path, params=params, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError("TiTiler preview request timed out")
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service")

    if resp.status_code != 200:
        logger.error("titiler_preview_failed path=%s status=%s", path, resp.status_code)
        raise RuntimeError(f"TiTiler preview failed: HTTP {resp.status_code}")

    return resp.content


async def get_collection_preview(
    collection_id: str,
    assets: str | None = None,
    *,
    bbox: list[float] | None = None,
    width: int = 512,
    height: int = 512,
    format: str = "png",
    rescale: str | None = None,
    colormap_name: str | None = None,
) -> bytes:
    """Fetch a preview image for a collection (or subset via bbox).

    Uses the /bbox/ endpoint to render a specific geographic area.

    Args:
        collection_id: STAC collection ID
        assets: Comma-separated asset names
        bbox: Bounding box [west, south, east, north] — required
        width: Output image width in pixels
        height: Output image height in pixels
        format: Image format ("png", "jpeg", "webp")
        rescale: Min,max rescale range
        colormap_name: Named colormap

    Returns:
        Raw image bytes
    """
    if not bbox:
        raise ValueError("bbox is required for collection preview")

    bbox_str = ",".join(str(v) for v in bbox)
    path = f"/collections/{collection_id}/bbox/{bbox_str}/{width}x{height}.{format}"

    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets
    if rescale:
        params["rescale"] = rescale
    if colormap_name:
        params["colormap_name"] = colormap_name

    client = _get_client()
    try:
        resp = await client.get(path, params=params, timeout=60.0)
    except httpx.TimeoutException:
        raise RuntimeError("TiTiler bbox request timed out")
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach TiTiler service")

    if resp.status_code != 200:
        logger.error("titiler_bbox_failed path=%s status=%s", path, resp.status_code)
        raise RuntimeError(f"TiTiler bbox request failed: HTTP {resp.status_code}")

    return resp.content


# ---------------------------------------------------------------------------
# Enhanced Streaming: Point Queries
# ---------------------------------------------------------------------------

async def get_item_point_value(
    collection_id: str,
    item_id: str,
    lon: float,
    lat: float,
    assets: str | None = None,
) -> dict[str, Any]:
    """Get pixel values at a specific point from a STAC item.

    Returns band values for all requested assets at the given coordinate.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID
        lon: Longitude (WGS84)
        lat: Latitude (WGS84)
        assets: Comma-separated asset names

    Returns:
        Dict with coordinates, band values, and asset info
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    return await _titiler_get(
        f"/collections/{collection_id}/items/{item_id}/point/{lon},{lat}",
        params=params,
    )


async def get_collection_point_value(
    collection_id: str,
    lon: float,
    lat: float,
    assets: str | None = None,
) -> dict[str, Any]:
    """Get pixel values at a specific point from a collection mosaic.

    Uses pgSTAC's mosaic logic to find the best item at the point.

    Args:
        collection_id: STAC collection ID
        lon: Longitude (WGS84)
        lat: Latitude (WGS84)
        assets: Comma-separated asset names

    Returns:
        Dict with coordinates, band values, and asset info
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    return await _titiler_get(
        f"/collections/{collection_id}/point/{lon},{lat}",
        params=params,
    )


async def get_mosaic_point_value(
    searchid: str,
    lon: float,
    lat: float,
    assets: str | None = None,
) -> dict[str, Any]:
    """Get pixel values at a specific point from a registered search mosaic.

    Args:
        searchid: pgSTAC search hash ID
        lon: Longitude (WGS84)
        lat: Latitude (WGS84)
        assets: Comma-separated asset names

    Returns:
        Dict with coordinates, band values, and asset info
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    return await _titiler_get(
        f"/searches/{searchid}/point/{lon},{lat}",
        params=params,
    )


# ---------------------------------------------------------------------------
# Enhanced Streaming: Statistics
# ---------------------------------------------------------------------------

async def get_item_statistics(
    collection_id: str,
    item_id: str,
    assets: str | None = None,
) -> dict[str, Any]:
    """Get band statistics for a STAC item.

    Returns min, max, mean, std, etc. for each band/asset.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID
        assets: Comma-separated asset names

    Returns:
        Dict with statistics per asset/band
    """
    params: dict[str, Any] = {}
    if assets:
        params["assets"] = assets

    return await _titiler_get(
        f"/collections/{collection_id}/items/{item_id}/statistics",
        params=params,
    )


async def get_item_info(
    collection_id: str,
    item_id: str,
) -> dict[str, Any]:
    """Get asset info for a STAC item.

    Returns available assets with their bounds, bands, data types, etc.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID

    Returns:
        Dict with asset information
    """
    return await _titiler_get(
        f"/collections/{collection_id}/items/{item_id}/info",
    )


async def get_item_bounds(
    collection_id: str,
    item_id: str,
) -> dict[str, Any]:
    """Get geographic bounds for a STAC item.

    Args:
        collection_id: STAC collection ID
        item_id: STAC item ID

    Returns:
        Dict with bounds [minx, miny, maxx, maxy]
    """
    return await _titiler_get(
        f"/collections/{collection_id}/items/{item_id}/bounds",
    )


# ---------------------------------------------------------------------------
# Convenience: Combined Item Mosaic (Multiple Specific Items)
# ---------------------------------------------------------------------------

async def get_items_mosaic_tilejson(
    collection_id: str,
    item_ids: list[str],
    assets: str | None = None,
    *,
    public_api_url: str | None = None,
) -> dict[str, Any]:
    """Get TileJSON for a mosaic of specific STAC items.

    This is the recommended way to visualize multiple specific items together.
    Internally registers a search (idempotent) and returns TileJSON.

    Args:
        collection_id: STAC collection ID
        item_ids: List of STAC item IDs to include
        assets: Comma-separated asset names
        public_api_url: Override the public API base URL

    Returns:
        TileJSON dict with rewritten tile URLs
    """
    searchid = await register_item_mosaic(item_ids, collection_ids=[collection_id])
    return await get_mosaic_tilejson(searchid, assets, public_api_url=public_api_url)
