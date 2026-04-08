from __future__ import annotations

from typing import Any

from shapely.geometry import box, mapping


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
