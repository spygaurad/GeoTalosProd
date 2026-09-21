"""Classifier tier: frozen DINOv2 CLS token -> one linear layer -> binary
logit ("is this patch the target class"). Deliberately simpler than the
detect tier (no feature pyramid, no anchor generation, no box regression) —
a classifier only ever claims something about the patch as a whole, so it
tolerates the sparse/partial labeling that poisons a detector's dense
per-pixel supervision. See AwakeForestProd's design discussion for why this
tier exists (bootstrap: classify -> scan -> human review -> only then train
a detector on the now-much-more-complete data).
"""
from __future__ import annotations

import torch
from torch import nn

from app.dinov2_backbone import load_dinov2_raw

INPUT_SIZE = 518  # same fixed input size as the detect tier — patch_size=14 grid


class ClassifyHead(nn.Module):
    out_channels = 1  # unused by anything outside this module; kept for symmetry with detect_model's backbone

    def __init__(self, backbone_key: str):
        super().__init__()
        model, hidden_dim, patch_size = load_dinov2_raw(backbone_key)
        self.dinov2 = model
        self.patch_size = patch_size
        self.linear = nn.Linear(hidden_dim, 1)

    def train(self, mode: bool = True) -> "ClassifyHead":
        super().train(mode)
        self.dinov2.eval()  # always frozen, regardless of outer training mode
        return self

    def extract_cls(self, x: torch.Tensor) -> torch.Tensor:
        """The frozen, expensive part only — cacheable across epochs, same
        reasoning as Dinov2FeaturePyramidBackbone.extract_tokens.
        """
        with torch.no_grad():
            out = self.dinov2(pixel_values=x)
            return out.last_hidden_state[:, 0, :]  # CLS token, (B, hidden_dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(self.extract_cls(x)).squeeze(-1)  # (B,) raw logits


def build_classifier(backbone_key: str) -> ClassifyHead:
    return ClassifyHead(backbone_key)
