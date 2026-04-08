from __future__ import annotations

from typing import Any

from app.automation.adapters.geo_utils import pixel_bbox_to_geo_polygon


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("coco adapter expects {'annotations': [...]}")
    category_map = {str(k): str(v) for k, v in (config.get("category_map") or {}).items()}
    min_score = float(config.get("min_score", 0.0))
    anns = raw.get("annotations") or []
    preds: list[dict[str, Any]] = []
    for ann in anns:
        score = float(ann.get("score", 1.0) or 1.0)
        if score < min_score:
            continue
        bbox = ann.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        geometry = pixel_bbox_to_geo_polygon(
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
            context=context,
        )
        cat = ann.get("category_id")
        label = category_map.get(str(cat), str(cat))
        preds.append(
            {
                "label": label,
                "confidence": score,
                "geometry": geometry,
                "properties": {"source": "coco", "category_id": cat},
            }
        )
    return {"format_version": "1.0", "predictions": preds, "metadata": {"adapter_used": "coco_to_platform"}}
