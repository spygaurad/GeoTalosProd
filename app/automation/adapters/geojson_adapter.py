from __future__ import annotations

from typing import Any


def convert(raw: Any, config: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        raise ValueError("geojson adapter expects a GeoJSON FeatureCollection")
    label_field = config.get("label_field", "label")
    score_field = config.get("score_field", "confidence")
    preds: list[dict[str, Any]] = []
    for feature in raw.get("features", []):
        props = feature.get("properties") or {}
        preds.append(
            {
                "label": str(props.get(label_field, "unknown")),
                "confidence": float(props.get(score_field, 1.0) or 1.0),
                "geometry": feature.get("geometry"),
                "properties": props,
            }
        )
    return {"format_version": "1.0", "predictions": preds, "metadata": {"adapter_used": "geojson_to_platform"}}
