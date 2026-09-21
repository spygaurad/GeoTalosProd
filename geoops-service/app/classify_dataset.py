"""Loads classifier training-runs manifests (geoops.dino_tasks classify
export) — simpler than the detect tier's: each item is just an image + a
0/1 label, no bbox/polygon targets file needed.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import torch
from PIL import Image

from app.classify_model import INPUT_SIZE
from app.storage import get_bytes, get_json


@dataclass
class ClassifyExample:
    image: torch.Tensor  # (3, INPUT_SIZE, INPUT_SIZE), 0-1 range
    label: float  # 1.0 = positive (target class), 0.0 = negative


def _load_image_tensor(org_id: str, key: str) -> torch.Tensor:
    raw = get_bytes(org_id, key)
    img = Image.open(io.BytesIO(raw)).convert("RGB").resize((INPUT_SIZE, INPUT_SIZE))
    arr = torch.from_numpy(np.array(img)).float() / 255.0
    return arr.permute(2, 0, 1)


def load_classify_split(org_id: str, s3_prefix: str, split: str) -> list[ClassifyExample]:
    manifest = get_json(org_id, f"{s3_prefix}/manifest.json")
    examples: list[ClassifyExample] = []
    for item in manifest["items"]:
        if item["split"] != split:
            continue
        examples.append(
            ClassifyExample(image=_load_image_tensor(org_id, item["image"]), label=float(item["label"]))
        )
    return examples
