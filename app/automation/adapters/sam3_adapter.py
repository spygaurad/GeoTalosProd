from __future__ import annotations

from typing import Any

from shapely.geometry import Polygon, shape

from app.automation.adapters.geo_utils import pixel_bbox_to_geo_polygon


def _to_geo_from_pixel_polygon(
    points: list[list[float]],
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    bbox = context.get("bbox")
    width = float(context.get("width") or 0)
    height = float(context.get("height") or 0)
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or width <= 0
        or height <= 0
        or len(points) < 3
    ):
        raise ValueError("SAM3 polygon conversion requires context bbox/width/height")

    min_lon, min_lat, max_lon, max_lat = [float(v) for v in bbox]
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat

    ring: list[tuple[float, float]] = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            continue
        x = float(pt[0])
        y = float(pt[1])
        lon = min_lon + (x / width) * lon_span
        lat = max_lat - (y / height) * lat_span
        ring.append((lon, lat))

    if len(ring) < 3:
        raise ValueError("SAM3 polygon has insufficient valid points")
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly.__geo_interface__


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Convert common SAM3-style outputs into platform prediction format.

    Supported structures:
    - {"predictions": [ ... ]}
    - {"instances": [ ... ]}
    - {"masks": [ ... ]}
    - [ ... ]  (list of prediction objects)

    Per-instance geometry priority:
    1) `geometry` (GeoJSON)
    2) `polygon`/configured polygon field (pixel points -> geo polygon)
    3) `bbox`/configured bbox field ([x, y, w, h] in pixels -> geo polygon)
    """
    if isinstance(raw, dict):
        entries = raw.get("predictions") or raw.get("instances") or raw.get("masks") or []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("sam3 adapter expects dict or list output")

    label_field = str(config.get("label_field", "label"))
    score_field = str(config.get("score_field", "score"))
    polygon_field = str(config.get("polygon_field", "polygon"))
    bbox_field = str(config.get("bbox_field", "bbox"))
    default_label = str(config.get("default_label", "object"))
    min_score = float(config.get("min_score", 0.0))

    preds: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        confidence = float(entry.get(score_field, entry.get("confidence", 1.0)) or 1.0)
        if confidence < min_score:
            continue

        label = str(entry.get(label_field, default_label))

        geom: dict[str, Any] | None = None
        if isinstance(entry.get("geometry"), dict):
            geom_candidate = shape(entry["geometry"])
            if not geom_candidate.is_valid:
                geom_candidate = geom_candidate.buffer(0)
            geom = geom_candidate.__geo_interface__
        elif isinstance(entry.get(polygon_field), list):
            try:
                geom = _to_geo_from_pixel_polygon(entry[polygon_field], context=context)
            except Exception:
                geom = None
        elif isinstance(entry.get(bbox_field), list) and len(entry[bbox_field]) == 4:
            bbox = entry[bbox_field]
            geom = pixel_bbox_to_geo_polygon(
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
                context=context,
            )

        if geom is None:
            continue

        props = dict(entry.get("properties") or {})
        props["source"] = "sam3"
        preds.append(
            {
                "label": label,
                "confidence": confidence,
                "geometry": geom,
                "properties": props,
            }
        )

    return {
        "format_version": "1.0",
        "predictions": preds,
        "metadata": {"adapter_used": "sam3_to_platform"},
    }
