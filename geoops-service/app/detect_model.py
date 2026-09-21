"""Builds a torchvision FCOS detector on top of a frozen DINOv2 backbone.

FCOS (anchor-free) chosen deliberately over a DETR-style head — DETR's
Hungarian-matching training dynamics are known to need lots of data even
with a frozen backbone, which defeats the point for 15-25 sample classes.
FCOS's per-pixel classification + regression target assignment converges
far more reliably at low sample counts. See AwakeForestProd's design notes
for this tier.
"""
from __future__ import annotations

from torchvision.models.detection.anchor_utils import AnchorGenerator
from torchvision.models.detection.fcos import FCOS

from app.dinov2_backbone import Dinov2FeaturePyramidBackbone

# Input images are resized to this fixed square before reaching the model
# (see preprocessing in train/infer) — divisible by DINOv2's patch_size=14.
INPUT_SIZE = 518


def build_fcos(backbone_key: str, num_classes: int) -> FCOS:
    """FCOS's classification head predicts num_classes logits directly (no
    explicit background class, unlike Faster R-CNN — "no object" is handled
    by the score threshold at inference, not a class slot). For our
    per-class head tier this is almost always num_classes=1 (the one class
    this head was trained for), but the API supports more if a head is ever
    trained for a small related group of classes.
    """
    backbone = Dinov2FeaturePyramidBackbone(backbone_key)
    # FCOS's default anchor_generator assumes 5 feature-map levels; our
    # pyramid produces 4 (p2..p5 — see feature_pyramid.py), so it must be
    # supplied explicitly with a matching level count. One "anchor" per
    # level (aspect_ratios=(1.0,)) is required by FCOS (it's anchor-free —
    # this only sets the box-size range each level is responsible for).
    anchor_generator = AnchorGenerator(
        sizes=((16,), (32,), (64,), (128,)),
        aspect_ratios=((1.0,),) * 4,
    )
    model = FCOS(
        backbone,
        num_classes=num_classes,
        min_size=INPUT_SIZE,
        max_size=INPUT_SIZE,
        anchor_generator=anchor_generator,
        # Pads batched images to a multiple of patch_size so the DINOv2 patch
        # grid always tiles evenly — see Dinov2FeaturePyramidBackbone.forward.
        size_divisible=14,
    )
    return model
