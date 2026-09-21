"""Loads a trained head artifact (produced by app.train.run_detect_training)
back onto its shared backbone — used by /eval and /infer, never re-saves
DINOv2's weights (they're already resident/cached from the backbone load).

A scan (ModelManager tiling one dataset item into patches) calls /infer once
per patch — anywhere from a few dozen to (now that scan tiles are sized to
the trained object's real-world footprint, not a generic 1024px default) up
to the platform's per-item patch cap. Re-downloading the artifact from S3 and
rebuilding the model from scratch on every one of those calls made a scan's
wall-clock cost scale with patch count for no reason, since the exact same
artifact is being loaded every time. Cache the built (eval-mode) model in
process memory, keyed by (org_id, artifact_key) — small in count (one per
trained head actually in use), unbounded growth isn't a real concern here.
"""
from __future__ import annotations

import io

import torch

from app.classify_model import build_classifier
from app.detect_model import build_fcos
from app.storage import get_bytes

_model_cache: dict[tuple[str, str, str], torch.nn.Module] = {}


def load_trained_detect_model(org_id: str, artifact_key: str) -> torch.nn.Module:
    cache_key = (str(org_id), artifact_key, "fcos")
    cached = _model_cache.get(cache_key)
    if cached is not None:
        return cached

    raw = get_bytes(org_id, artifact_key)
    artifact = torch.load(io.BytesIO(raw), map_location="cpu", weights_only=False)
    if artifact.get("kind") != "fcos":
        raise ValueError(f"Artifact kind '{artifact.get('kind')}' is not 'fcos'")

    model = build_fcos(artifact["backbone_key"], num_classes=1)
    model.backbone.pyramid.load_state_dict(artifact["pyramid_state_dict"])
    model.head.load_state_dict(artifact["head_state_dict"])
    model.eval()
    _model_cache[cache_key] = model
    return model


def load_trained_classify_model(org_id: str, artifact_key: str) -> torch.nn.Module:
    cache_key = (str(org_id), artifact_key, "classify")
    cached = _model_cache.get(cache_key)
    if cached is not None:
        return cached

    raw = get_bytes(org_id, artifact_key)
    artifact = torch.load(io.BytesIO(raw), map_location="cpu", weights_only=False)
    if artifact.get("kind") != "classify":
        raise ValueError(f"Artifact kind '{artifact.get('kind')}' is not 'classify'")

    model = build_classifier(artifact["backbone_key"])
    model.linear.load_state_dict(artifact["linear_state_dict"])
    model.eval()
    _model_cache[cache_key] = model
    return model
