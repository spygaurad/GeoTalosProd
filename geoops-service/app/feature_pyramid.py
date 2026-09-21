"""ViTDet-style "simple feature pyramid": turns a single-scale plain-ViT
patch-token grid into the multi-scale feature dict torchvision's detection
heads (FCOS, RetinaNet, Faster R-CNN) expect.

Only this small adapter is ported from the ViTDet idea (Detectron2's
modeling/backbone/vit.py) — not the framework. DINOv2 outputs one
resolution (H/patch_size x W/patch_size); detection heads want several
scales to handle objects of different sizes.
"""
from __future__ import annotations

from collections import OrderedDict

import torch
from torch import nn


class SimpleFeaturePyramid(nn.Module):
    """(B, in_channels, H, W) -> {"p2": .., "p3": .., "p4": .., "p5": ..}

    Strides relative to the *input grid* (not the original image): p2=1/2
    (upsampled), p3=1x, p4=2x downsampled, p5=4x downsampled. Combined with
    a patch_size-14 grid, that's effective strides of 7/14/28/56 pixels in
    the original (resized) image — a reasonable spread for small-to-large
    objects in a single training crop.
    """

    def __init__(self, in_channels: int = 768, out_channels: int = 256):
        super().__init__()
        self.out_channels = out_channels

        self.up2 = nn.Sequential(
            nn.ConvTranspose2d(in_channels, in_channels // 2, kernel_size=2, stride=2),
            nn.GroupNorm(32, in_channels // 2),
            nn.GELU(),
            nn.Conv2d(in_channels // 2, out_channels, kernel_size=1),
        )
        self.same = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        self.down2 = nn.Sequential(
            nn.MaxPool2d(kernel_size=2, stride=2),
            nn.Conv2d(in_channels, out_channels, kernel_size=1),
        )
        self.down4 = nn.Sequential(
            nn.MaxPool2d(kernel_size=4, stride=4),
            nn.Conv2d(in_channels, out_channels, kernel_size=1),
        )
        self.smooth = nn.ModuleDict(
            {name: nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1) for name in ("p2", "p3", "p4", "p5")}
        )

    def forward(self, x: torch.Tensor) -> "OrderedDict[str, torch.Tensor]":
        feats = {
            "p2": self.up2(x),
            "p3": self.same(x),
            "p4": self.down2(x),
            "p5": self.down4(x),
        }
        return OrderedDict((name, self.smooth[name](feat)) for name, feat in feats.items())
