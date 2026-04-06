from __future__ import annotations

from typing import Any

from app.automation.adapters.geo_utils import normalized_bbox_to_geo_polygon


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, list):
        raise ValueError("yolo adapter expects list rows: [class_id, cx, cy, w, h, conf]")
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
        geometry = normalized_bbox_to_geo_polygon(float(cx), float(cy), float(w), float(h), context=context)
        preds.append(
            {
                "label": label,
                "confidence": score,
                "geometry": geometry,
                "properties": {"source": "yolo"},
            }
        )
    return {"format_version": "1.0", "predictions": preds, "metadata": {"adapter_used": "yolo_to_platform"}}
