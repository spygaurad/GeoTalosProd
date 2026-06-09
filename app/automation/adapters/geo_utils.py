from __future__ import annotations

from typing import Any

from shapely.geometry import Point, box, mapping


def _bounds_from_context(context: dict[str, Any]) -> tuple[float, float, float, float]:
    bbox = context.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    raise ValueError("Missing bbox in dataset item context")


def normalized_bbox_to_geo_polygon(
    cx: float,
    cy: float,
    w: float,
    h: float,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = _bounds_from_context(context)
    x1 = cx - (w / 2.0)
    y1 = cy - (h / 2.0)
    x2 = cx + (w / 2.0)
    y2 = cy + (h / 2.0)
    lon1 = min_lon + x1 * (max_lon - min_lon)
    lat1 = max_lat - y1 * (max_lat - min_lat)
    lon2 = min_lon + x2 * (max_lon - min_lon)
    lat2 = max_lat - y2 * (max_lat - min_lat)
    return mapping(box(lon1, min(lat1, lat2), lon2, max(lat1, lat2)))


def pixel_bbox_to_geo_polygon(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    width = float(context.get("width") or 0)
    height = float(context.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Missing width/height in dataset item context")
    cx = (x + (w / 2.0)) / width
    cy = (y + (h / 2.0)) / height
    return normalized_bbox_to_geo_polygon(cx, cy, w / width, h / height, context=context)


def geo_bbox_to_pixel_bbox(
    geo_bbox: list[float],
    *,
    context: dict[str, Any],
) -> list[float] | None:
    """Project a geo (EPSG:4326) bbox into a patch's pixel space (xyxy).

    Inverse of :func:`pixel_bbox_to_geo_polygon`. Clips ``geo_bbox``
    ``[min_lon, min_lat, max_lon, max_lat]`` to the patch geo-bounds, then maps
    to top-left-origin pixel coordinates using the patch ``width``/``height``::

        x = (lon - min_lon) / lon_span * width
        y = (max_lat - lat) / lat_span * height   # north maps to y=0

    Returns ``[x1, y1, x2, y2]`` (x1<x2, y1<y2) clamped to the patch, or
    ``None`` when the box does not overlap the patch (empty intersection) — the
    signal ModelManager uses to skip a patch for a spatial prompt.
    """
    min_lon, min_lat, max_lon, max_lat = _bounds_from_context(context)
    width = float(context.get("width") or 0)
    height = float(context.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Missing width/height in dataset item context")
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat
    if lon_span <= 0 or lat_span <= 0:
        return None

    g_min_lon, g_min_lat, g_max_lon, g_max_lat = (float(v) for v in geo_bbox)
    # Clip to the patch bounds.
    c_min_lon = max(min_lon, min(g_min_lon, g_max_lon))
    c_max_lon = min(max_lon, max(g_min_lon, g_max_lon))
    c_min_lat = max(min_lat, min(g_min_lat, g_max_lat))
    c_max_lat = min(max_lat, max(g_min_lat, g_max_lat))
    if c_min_lon >= c_max_lon or c_min_lat >= c_max_lat:
        return None  # no overlap with this patch

    x1 = (c_min_lon - min_lon) / lon_span * width
    x2 = (c_max_lon - min_lon) / lon_span * width
    # North (max lat) is the top of the image (y=0), so the smaller y comes
    # from the larger latitude.
    y1 = (max_lat - c_max_lat) / lat_span * height
    y2 = (max_lat - c_min_lat) / lat_span * height

    x1 = max(0.0, min(width, x1))
    x2 = max(0.0, min(width, x2))
    y1 = max(0.0, min(height, y1))
    y2 = max(0.0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def pixel_point_to_geo_point(
    x: float,
    y: float,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Convert a single patch-pixel point (top-left origin) to a GeoJSON Point.

    Uses the patch geo-bounds (``bbox``) and pixel dimensions (``width`` /
    ``height``) from the inference context — the same georeferencing basis the
    bbox/polygon helpers use.
    """
    min_lon, min_lat, max_lon, max_lat = _bounds_from_context(context)
    width = float(context.get("width") or 0)
    height = float(context.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Missing width/height in dataset item context")
    lon = min_lon + (x / width) * (max_lon - min_lon)
    lat = max_lat - (y / height) * (max_lat - min_lat)
    return mapping(Point(lon, lat))
