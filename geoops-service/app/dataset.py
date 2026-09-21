"""Loads the training-runs/{job_id}/manifest.json + crops + targets
geoops.dino_tasks.run_dino_finetune_export wrote to S3/MinIO, into the
tensor format FCOS expects.

Normalized targets (cx, cy, w, h in [0,1], relative to the exported crop)
are resolution-independent, so scaling by INPUT_SIZE after resizing the
image to INPUT_SIZE is exact — no need to track the crop's original pixel
dimensions here.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import torch
from PIL import Image

from app.detect_model import INPUT_SIZE
from app.storage import get_bytes, get_json


@dataclass
class Example:
    image: torch.Tensor  # (3, INPUT_SIZE, INPUT_SIZE), 0-1 range
    boxes: torch.Tensor  # (N, 4) x1,y1,x2,y2 in pixel space
    labels: torch.Tensor  # (N,) all zeros — single-class head


def _load_image_tensor(org_id: str, key: str) -> torch.Tensor:
    raw = get_bytes(org_id, key)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE))
    arr = torch.from_numpy(np.array(img)).float() / 255.0  # H,W,3
    return arr.permute(2, 0, 1)  # 3,H,W


def _bbox_target_to_pixels(target: dict) -> list[float] | None:
    bbox = target.get("bbox")
    if bbox is None:
        return None
    cx, cy, w, h = bbox
    x1, y1 = (cx - w / 2) * INPUT_SIZE, (cy - h / 2) * INPUT_SIZE
    x2, y2 = (cx + w / 2) * INPUT_SIZE, (cy + h / 2) * INPUT_SIZE
    x1, x2 = max(0.0, min(x1, x2)), min(float(INPUT_SIZE), max(x1, x2))
    y1, y2 = max(0.0, min(y1, y2)), min(float(INPUT_SIZE), max(y1, y2))
    # FCOS's target assignment assumes strictly positive area.
    if x2 - x1 < 1.0 or y2 - y1 < 1.0:
        return None
    return [x1, y1, x2, y2]


def load_split(org_id: str, s3_prefix: str, split: str) -> list[Example]:
    manifest = get_json(org_id, f"{s3_prefix}/manifest.json")
    task = manifest.get("task", "detect")
    examples: list[Example] = []
    for item in manifest["items"]:
        if item["split"] != split:
            continue
        targets_doc = get_json(org_id, item["targets"])
        boxes: list[list[float]] = []
        for t in targets_doc.get("targets", []):
            if task != "detect":
                continue  # segmentation targets aren't consumed here
            px = _bbox_target_to_pixels(t)
            if px is not None:
                boxes.append(px)
        # A crop can legitimately have zero targets — a background-only tile
        # from an exhaustively-labeled AOI (geoops.dino_tasks's AOI tiling).
        # Keep it as a real negative example instead of dropping it: FCOS
        # supports empty-target images natively, and a detector trained with
        # no negatives at all has no way to learn what "not the object"
        # looks like.
        boxes_tensor = (
            torch.tensor(boxes, dtype=torch.float32) if boxes else torch.zeros((0, 4), dtype=torch.float32)
        )
        examples.append(
            Example(
                image=_load_image_tensor(org_id, item["image"]),
                boxes=boxes_tensor,
                labels=torch.zeros(len(boxes), dtype=torch.int64),
            )
        )
    return examples
