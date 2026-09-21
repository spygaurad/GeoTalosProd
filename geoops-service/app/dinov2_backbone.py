"""DINOv2 loading + the nn.Module wrapper torchvision's FCOS/detection heads
require as their `backbone` argument (needs a `.out_channels` attribute and
must return an OrderedDict of multi-scale feature maps from a normalized
image batch).

Frozen by construction: DINOv2's parameters are set requires_grad=False at
load time, and this wrapper overrides train() so calling .train() on the
outer detection model (standard PyTorch training-loop pattern) never flips
DINOv2 back into train mode — only the feature pyramid + head are ever
trainable. That's the whole point of this tier: share one frozen backbone,
train only small per-class heads on top.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import torch
from torch import nn
from transformers import AutoModel

from app.backbones import BACKBONES
from app.config import settings
from app.feature_pyramid import SimpleFeaturePyramid

_lock = threading.Lock()
_loaded_raw: dict[str, tuple[nn.Module, int, int]] = {}  # key -> (model, hidden_dim, patch_size)


def load_dinov2_raw(key: str) -> tuple[nn.Module, int, int]:
    """Load (or return cached) the raw HF DINOv2 model, frozen. Returns
    (model, hidden_dim, patch_size).
    """
    if key not in BACKBONES:
        raise KeyError(f"Unknown backbone '{key}'")
    with _lock:
        if key in _loaded_raw:
            return _loaded_raw[key]
        cfg = BACKBONES[key]
        model = AutoModel.from_pretrained(cfg["model_id"], cache_dir=settings.MODEL_CACHE_DIR)
        model.eval()
        for p in model.parameters():
            p.requires_grad = False
        entry = (model, cfg["hidden_dim"], cfg["patch_size"])
        _loaded_raw[key] = entry
        return entry


class Dinov2FeaturePyramidBackbone(nn.Module):
    """Frozen DINOv2 + trainable SimpleFeaturePyramid, wrapped for
    torchvision's detection models. Input: (B, 3, H, W) already normalized
    by the detection model's own GeneralizedRCNNTransform (ImageNet
    mean/std — same stats DINOv2 expects, so no double-normalization). H
    and W must each be divisible by patch_size; the caller (FCOS's
    transform, constructed with size_divisible=patch_size) guarantees that.
    """

    def __init__(self, backbone_key: str, pyramid_out_channels: int = 256):
        super().__init__()
        model, hidden_dim, patch_size = load_dinov2_raw(backbone_key)
        self.dinov2 = model
        self.patch_size = patch_size
        self.pyramid = SimpleFeaturePyramid(in_channels=hidden_dim, out_channels=pyramid_out_channels)
        self.out_channels = pyramid_out_channels

    def train(self, mode: bool = True) -> "Dinov2FeaturePyramidBackbone":
        super().train(mode)
        self.dinov2.eval()  # always frozen, regardless of outer training mode
        return self

    def extract_tokens(self, x: torch.Tensor) -> torch.Tensor:
        """The frozen, expensive part only — DINOv2 forward pass, no pyramid.

        Deterministic given x (DINOv2 is frozen), so this is the piece worth
        computing once and caching across training epochs rather than
        re-running on every step — see app.train.run_detect_training.
        """
        with torch.no_grad():
            # This transformers version's Dinov2Embeddings always
            # interpolates position embeddings to the input's actual H/W
            # (no explicit flag needed/accepted) — handles our non-pretrained
            # input size (518x518) automatically.
            out = self.dinov2(pixel_values=x)
            tokens = out.last_hidden_state[:, 1:, :]  # drop CLS token
        b, n, c = tokens.shape
        h_grid = x.shape[2] // self.patch_size
        w_grid = x.shape[3] // self.patch_size
        if h_grid * w_grid != n:
            raise ValueError(
                f"Patch-token count {n} doesn't match grid {h_grid}x{w_grid} — "
                f"input H/W must be divisible by patch_size={self.patch_size}"
            )
        return tokens.permute(0, 2, 1).reshape(b, c, h_grid, w_grid)

    def forward(self, x: torch.Tensor) -> "OrderedDict[str, torch.Tensor]":
        return self.pyramid(self.extract_tokens(x))

    def trainable_parameters(self):
        """Only the pyramid — used to build the optimizer so DINOv2 is
        never touched even if someone forgets to check requires_grad.
        """
        return self.pyramid.parameters()
