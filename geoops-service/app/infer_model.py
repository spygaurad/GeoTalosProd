"""Detect inference: runs a trained FCOS head, converts pixel-space
predictions back into real-world EPSG:4326 GeoJSON using the same
georef_metadata.bbox convention app.services.model_manager.ModelManager
already sends to every model endpoint — this is the inverse of
geoops.yolo_service.geometry_to_yolo_bbox's math (real-world -> normalized
pixel), so predictions round-trip through the same coordinate convention
the export pipeline used to build training targets.

Output shape ({"predictions": [{"label", "confidence", "geometry"}]}) is
exactly what app.automation.adapters.platform_adapter (the
"platform_passthrough" adapter) expects — geoops-service needs no custom
adapter, and ModelManager needs zero changes to call a DINOv2-tier head.
"""
from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
import torch
from PIL import Image

from app.detect_model import INPUT_SIZE


def _pixel_box_to_geojson(box: list[float], item_bbox: list[float]) -> dict[str, Any]:
    x1, y1, x2, y2 = box
    ix0, iy0, ix1, iy1 = item_bbox
    span_x, span_y = ix1 - ix0, iy1 - iy0
    nx1, nx2 = x1 / INPUT_SIZE, x2 / INPUT_SIZE
    ny1, ny2 = y1 / INPUT_SIZE, y2 / INPUT_SIZE
    lon1, lon2 = ix0 + nx1 * span_x, ix0 + nx2 * span_x
    # Pixel row 0 is the image's top (maxy) — same flip as
    # geoops.yolo_service.geometry_to_yolo_bbox, inverted here.
    lat1, lat2 = iy1 - ny2 * span_y, iy1 - ny1 * span_y
    minx, maxx = sorted((lon1, lon2))
    miny, maxy = sorted((lat1, lat2))
    return {
        "type": "Polygon",
        "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
    }


def run_detect_inference(
    model: torch.nn.Module,
    image_base64: str,
    georef_metadata: dict[str, Any],
    *,
    class_label: str,
    score_thresh: float = 0.3,
) -> list[dict[str, Any]]:
    item_bbox = georef_metadata.get("bbox")
    if not isinstance(item_bbox, list) or len(item_bbox) != 4:
        raise ValueError("georef_metadata.bbox [minx, miny, maxx, maxy] is required")

    raw = base64.b64decode(image_base64)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE))
    arr = torch.from_numpy(np.array(img)).float() / 255.0
    tensor = arr.permute(2, 0, 1)

    model.eval()
    with torch.no_grad():
        pred = model([tensor])[0]

    predictions: list[dict[str, Any]] = []
    for box, score in zip(pred["boxes"].tolist(), pred["scores"].tolist(), strict=True):
        if score < score_thresh:
            continue
        predictions.append(
            {
                "label": class_label,
                "confidence": float(score),
                "geometry": _pixel_box_to_geojson(box, item_bbox),
            }
        )
    return predictions
