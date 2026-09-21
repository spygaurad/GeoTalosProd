"""Geographic scale helpers for the embedding bank.

Every embedding is a crop of a real-world area squeezed into the same
``crop_size_px`` pixel grid — two crops of very different real-world size
look nothing alike to an embedding model even if the underlying content is
similar (a 5m object and a 500m object both resized to 256x256 are encoding
completely different things). These helpers convert a GeoJSON geometry's
bbox into a real-world span (meters) and snap it to one of a small number of
scale tiers, so ``search()`` only ever compares same-tier candidates and
``aoi_scan`` tiles a dataset item at the same scale as its reference.

``DatasetItem``/``Embedding`` geometries are plain GeoJSON (JSONB), not
PostGIS columns, so this uses a simple equirectangular approximation
(accurate enough for tiling/tiering, not for precise area math) rather than a
geodesic library — consistent with the "no PostGIS needed for simple bbox
math" choice already made for ``DatasetItem.geometry``.
"""
from __future__ import annotations

import math
from typing import Any

from shapely.geometry import shape

# Log-spaced default tiers (meters), covering roughly a car to a large fire
# perimeter. Sanity-check against real annotation bbox percentiles once the
# DB is reachable (see geoops/README.md for the query) and adjust if the
# actual distribution warrants different boundaries.
DEFAULT_TIERS_M: list[float] = [10.0, 40.0, 160.0, 640.0, 2560.0]

_METERS_PER_DEGREE_LAT = 110_540.0
_METERS_PER_DEGREE_LON_AT_EQUATOR = 111_320.0


def _bbox_of(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = shape(geometry).bounds
    return float(minx), float(miny), float(maxx), float(maxy)


def bbox_coords_span_m(bbox: list[float]) -> float:
    """Longer side (width or height) of an EPSG:4326 ``[minx, miny, maxx, maxy]``
    bbox, in meters. Same math as ``bbox_span_m`` but takes a plain bbox
    instead of a GeoJSON geometry, for callers that already have one (e.g. a
    training crop's padded bbox, which never had a Shapely geometry).
    """
    minx, miny, maxx, maxy = bbox
    mid_lat_rad = math.radians((miny + maxy) / 2.0)
    width_m = (maxx - minx) * _METERS_PER_DEGREE_LON_AT_EQUATOR * math.cos(mid_lat_rad)
    height_m = (maxy - miny) * _METERS_PER_DEGREE_LAT
    return max(abs(width_m), abs(height_m))


def bbox_span_m(geometry: dict[str, Any]) -> float:
    """Longer side (width or height) of ``geometry``'s bbox, in meters."""
    return bbox_coords_span_m(list(_bbox_of(geometry)))


def meters_to_degrees(span_m: float, mid_lat_deg: float) -> tuple[float, float]:
    """(lon_degrees, lat_degrees) spanning ``span_m`` meters at latitude
    ``mid_lat_deg`` — the inverse of ``bbox_coords_span_m``'s per-axis math,
    for callers that need to build a real-world-sized bbox from a center
    point rather than measure an existing one.
    """
    mid_lat_rad = math.radians(mid_lat_deg)
    lon_deg = span_m / (_METERS_PER_DEGREE_LON_AT_EQUATOR * math.cos(mid_lat_rad) or 1e-9)
    lat_deg = span_m / _METERS_PER_DEGREE_LAT
    return lon_deg, lat_deg


def nearest_tier(span_m: float, tiers: list[float] = DEFAULT_TIERS_M) -> int:
    """Index of the tier whose size is closest to ``span_m`` in log-space."""
    if span_m <= 0:
        return 0
    log_span = math.log(span_m)
    diffs = [abs(log_span - math.log(t)) for t in tiers]
    return diffs.index(min(diffs))


def canonical_tiles(
    item_bbox: list[float],
    tier: int,
    clip_bbox: list[float] | None = None,
    tiers: list[float] = DEFAULT_TIERS_M,
) -> list[tuple[int, int, list[float]]]:
    """Deterministic, non-overlapping tile grid for one item at one tier.

    Tiles are anchored to ``item_bbox``'s own origin (not the AOI's), so the
    same ``(item, tier, col, row)`` always maps to the same geographic tile
    across separate scan requests — this is what lets ``embedding_tiles`` act
    as a cache: re-scanning an overlapping AOI hits the same (col, row) keys
    instead of generating new offsets every time.
    """
    minx, miny, maxx, maxy = item_bbox
    mid_lat_rad = math.radians((miny + maxy) / 2.0)
    tier_span_m = tiers[max(0, min(tier, len(tiers) - 1))]
    tile_h_deg = tier_span_m / _METERS_PER_DEGREE_LAT
    lon_scale = math.cos(mid_lat_rad) or 1e-9
    tile_w_deg = tier_span_m / (_METERS_PER_DEGREE_LON_AT_EQUATOR * lon_scale)

    scan_minx, scan_miny, scan_maxx, scan_maxy = minx, miny, maxx, maxy
    if clip_bbox is not None:
        scan_minx = max(scan_minx, clip_bbox[0])
        scan_miny = max(scan_miny, clip_bbox[1])
        scan_maxx = min(scan_maxx, clip_bbox[2])
        scan_maxy = min(scan_maxy, clip_bbox[3])
        if scan_minx >= scan_maxx or scan_miny >= scan_maxy:
            return []

    col_start = math.floor((scan_minx - minx) / tile_w_deg)
    col_end = math.ceil((scan_maxx - minx) / tile_w_deg)
    row_start = math.floor((scan_miny - miny) / tile_h_deg)
    row_end = math.ceil((scan_maxy - miny) / tile_h_deg)

    tiles: list[tuple[int, int, list[float]]] = []
    for row in range(row_start, row_end):
        tile_miny = miny + row * tile_h_deg
        tile_maxy = tile_miny + tile_h_deg
        if tile_maxy <= scan_miny or tile_miny >= scan_maxy:
            continue
        for col in range(col_start, col_end):
            tile_minx = minx + col * tile_w_deg
            tile_maxx = tile_minx + tile_w_deg
            if tile_maxx <= scan_minx or tile_minx >= scan_maxx:
                continue
            tiles.append((col, row, [tile_minx, tile_miny, tile_maxx, tile_maxy]))
    return tiles


def cosine_distance(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Embedding dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - (dot / (norm_a * norm_b))
