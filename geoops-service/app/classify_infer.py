"""Classifier inference. Plugs into the exact same patch-tiled scanning
ModelManager already does for the detect tier — the only difference is a
classifier makes one whole-patch call ("is this class present here"), so a
positive prediction's geometry is the *whole patch's* bbox, not a
regressed box within it. Output shape still matches
app.automation.adapters.platform_adapter's expected
{"predictions": [{"label", "confidence", "geometry"}]}, so ModelManager
needs no changes for this tier either.
"""
from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
import torch
from PIL import Image

from app.classify_model import INPUT_SIZE


def run_classify_inference(
    model: torch.nn.Module,
    image_base64: str,
    georef_metadata: dict[str, Any],
    *,
    class_label: str,
    score_thresh: float = 0.5,
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
        logit = model(tensor.unsqueeze(0))[0]
        score = torch.sigmoid(logit).item()

    if score < score_thresh:
        return []

    minx, miny, maxx, maxy = item_bbox
    geometry = {
        "type": "Polygon",
        "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
    }
    return [{"label": class_label, "confidence": float(score), "geometry": geometry}]
