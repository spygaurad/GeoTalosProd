from __future__ import annotations

from typing import Any

from shapely.geometry import shape

from app.automation.adapters.geo_utils import (
    normalized_bbox_to_geo_polygon,
    pixel_bbox_to_geo_polygon,
)


def _convert_instances(
    entries: list[Any], config: dict[str, Any], context: dict[str, Any]
) -> list[dict[str, Any]]:
    """Handle ``{instances: [{label, score, polygon, bbox}]}`` style outputs.

    palm_api's ``/predict/yolo/platform`` endpoint emits this shape — every
    detection has a 4-point pixel-space polygon (the bbox corners) plus
    ``[x, y, w, h]`` bbox fallback. Used so the YOLO adapter doesn't force
    callers to re-register the YOLO endpoint under ``sam3_to_platform``.
    """
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
            shp = shape(entry["geometry"])
            if not shp.is_valid:
                shp = shp.buffer(0)
            geom = shp.__geo_interface__
        elif isinstance(entry.get(polygon_field), list) and len(entry[polygon_field]) >= 3:
            from app.automation.adapters.sam3_adapter import _to_geo_from_pixel_polygon
            try:
                geom = _to_geo_from_pixel_polygon(entry[polygon_field], context=context)
            except Exception:
                geom = None
        if geom is None and isinstance(entry.get(bbox_field), list) and len(entry[bbox_field]) == 4:
            bb = entry[bbox_field]
            geom = pixel_bbox_to_geo_polygon(
                float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]), context=context
            )
        if geom is None:
            continue

        props = dict(entry.get("properties") or {})
        props["source"] = "yolo"
        preds.append(
            {"label": label, "confidence": confidence, "geometry": geom, "properties": props}
        )
    return preds


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Normalize YOLO outputs to platform predictions.

    Supports two response shapes for the same adapter so users don't need to
    juggle adapters when switching between raw-YOLO and platform endpoints:
    - Raw rows: ``[[class_id, cx, cy, w, h, conf], ...]`` (legacy /predict/upload).
    - Platform instances: ``{instances: [{label, score, polygon, bbox}, ...]}``
      (palm_api's ``/predict/yolo/platform``).
    """
    # Platform-instances shape (matches sam3_to_platform output contract).
    if isinstance(raw, dict):
        entries = raw.get("instances") or raw.get("predictions") or raw.get("detections")
        if isinstance(entries, list):
            return {
                "format_version": "1.0",
                "predictions": _convert_instances(entries, config, context),
                "metadata": {"adapter_used": "yolo_to_platform", "input_shape": "instances"},
            }

    # Raw YOLO rows fallback.
    if not isinstance(raw, list):
        raise ValueError(
            "yolo adapter expects either {instances: [...]} or list rows "
            "[class_id, cx, cy, w, h, conf]"
        )
    category_map = {str(k): str(v) for k, v in (config.get("category_map") or {}).items()}
    min_score = float(config.get("min_score", 0.0))
    preds: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        class_id, cx, cy, w, h, conf = row[:6]
        score = float(conf)
        if score < min_score:
            continue
        label = category_map.get(str(int(class_id)), str(class_id))
        geometry = normalized_bbox_to_geo_polygon(
            float(cx), float(cy), float(w), float(h), context=context
        )
        preds.append(
            {
                "label": label,
                "confidence": score,
                "geometry": geometry,
                "properties": {"source": "yolo"},
            }
        )
    return {
        "format_version": "1.0",
        "predictions": preds,
        "metadata": {"adapter_used": "yolo_to_platform", "input_shape": "rows"},
    }
