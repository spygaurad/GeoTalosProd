from __future__ import annotations

from typing import Any

from app.automation.adapters.geo_utils import pixel_point_to_geo_point


def convert(raw: Any, config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Normalize crown-detection outputs to platform predictions as Points.

    Expects palm_api's ``/predict/crown/platform`` shape::

        {"instances": [{"label", "score", "point": [x, y], "bbox": [x, y, w, h]}, ...]}

    Each instance becomes a GeoJSON ``Point`` at the crown centre. ``point`` is
    in patch-pixel space (top-left origin) and is georeferenced against the patch
    bbox in ``context``. When ``point`` is absent the bbox centre is used.

    ``category_map`` (optional) remaps the model-emitted label to a schema class
    name by ``str(int(label_or_id))`` first, then by raw string — mirroring the
    YOLO adapter so the same config style works for either.
    """
    if isinstance(raw, dict):
        entries = raw.get("instances") or raw.get("predictions") or raw.get("detections") or []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError(
            "crown adapter expects {instances: [...]} or a list of instances"
        )

    label_field = str(config.get("label_field", "label"))
    score_field = str(config.get("score_field", "score"))
    point_field = str(config.get("point_field", "point"))
    bbox_field = str(config.get("bbox_field", "bbox"))
    default_label = str(config.get("default_label", "object"))
    min_score = float(config.get("min_score", 0.0))
    category_map = {str(k): str(v) for k, v in (config.get("category_map") or {}).items()}

    def _remap(label: str) -> str:
        if label in category_map:
            return category_map[label]
        try:
            keyed = str(int(float(label)))
        except (TypeError, ValueError):
            keyed = None
        if keyed is not None and keyed in category_map:
            return category_map[keyed]
        return label

    preds: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        confidence = float(entry.get(score_field, entry.get("confidence", 1.0)) or 1.0)
        if confidence < min_score:
            continue

        raw_label = str(entry.get(label_field, default_label))
        label = _remap(raw_label)

        # Resolve the pixel point: explicit point field, else bbox centre.
        px: float | None = None
        py: float | None = None
        pt = entry.get(point_field)
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            px, py = float(pt[0]), float(pt[1])
        else:
            bb = entry.get(bbox_field)
            if isinstance(bb, (list, tuple)) and len(bb) == 4:
                px = float(bb[0]) + float(bb[2]) / 2.0
                py = float(bb[1]) + float(bb[3]) / 2.0
        if px is None or py is None:
            continue

        try:
            geom = pixel_point_to_geo_point(px, py, context=context)
        except Exception:
            continue

        props = dict(entry.get("properties") or {})
        props["source"] = "crown"
        preds.append(
            {"label": label, "confidence": confidence, "geometry": geom, "properties": props}
        )

    return {
        "format_version": "1.0",
        "predictions": preds,
        "metadata": {"adapter_used": "crown_to_platform", "input_shape": "instances"},
    }
