"""Real training loop for the detect (FCOS) tier — CPU for now, per
decision: small per-class heads on a frozen backbone are cheap enough on
CPU; GPU is deferred until the YOLO tier needs it.

Optimizer only ever sees pyramid + head parameters — DINOv2 is frozen at
load time (dinov2_backbone.load_dinov2_raw) and never touches the
optimizer, so there's no way for a training run to accidentally update it.

Feature caching: DINOv2 is frozen, so its output for a given image is
IDENTICAL every epoch — recomputing that 86M/21M-param forward pass from
scratch on every step (as a plain `model(images, targets)` call would) is
pure waste. Every training image's frozen tokens are extracted once up
front (one pass through DINOv2 total, not one per epoch), then each epoch
only re-runs the actually-trainable pyramid + FCOS head against those
cached tokens. This reproduces torchvision FCOS.forward()'s train-mode
internals by hand (transform -> backbone -> head -> anchors -> loss) split
across a one-time cache step and a per-step fast path — see
_cache_examples/_train_step below, cross-checked against
torchvision.models.detection.fcos.FCOS.forward's actual source.
"""
from __future__ import annotations

import io
import logging
import random

import torch
from torchvision.models.detection.image_list import ImageList

from app.dataset import load_split
from app.detect_model import build_fcos
from app.storage import put_bytes

logger = logging.getLogger(__name__)

BATCH_SIZE = 4


def _cache_examples(model, examples: list) -> list[dict]:
    """One pass through the frozen backbone per example. Returns cached
    dicts: {tokens (C,H,W), image_size, target}. image_size/target come from
    model.transform so they're in the exact coordinate space FCOS's loss
    expects — identical to what the normal model(images, targets) path would
    produce, just computed once instead of every epoch.
    """
    cached = []
    with torch.no_grad():
        for ex in examples:
            images, targets = model.transform([ex.image], [{"boxes": ex.boxes, "labels": ex.labels}])
            tokens = model.backbone.extract_tokens(images.tensors)  # (1, C, H, W)
            cached.append(
                {
                    "tokens": tokens[0],
                    "image_size": images.image_sizes[0],
                    "target": targets[0],
                }
            )
    return cached


def _train_step(model, batch: list[dict]) -> dict[str, torch.Tensor]:
    """The fast, trainable-only path: pyramid -> head -> anchors -> loss,
    starting from already-cached frozen tokens instead of raw images. Mirrors
    FCOS.forward()'s train branch exactly (same method calls, same argument
    shapes) minus the transform+backbone step, which _cache_examples already
    did once.
    """
    tokens_batch = torch.stack([b["tokens"] for b in batch])  # (B, C, H, W)
    targets_batch = [b["target"] for b in batch]
    image_sizes = [b["image_size"] for b in batch]

    pyramid_features = model.backbone.pyramid(tokens_batch)
    features = list(pyramid_features.values())
    head_outputs = model.head(features)

    # AnchorGenerator only reads .tensors.shape[-2:] and len(.image_sizes) —
    # never the tensor's actual content — so a shape-only placeholder is
    # exactly as correct as the real (already-normalized) image batch would
    # be here, and avoids keeping the raw images around just for this.
    dummy_tensors = torch.empty(len(batch), 1, *image_sizes[0])
    image_list = ImageList(dummy_tensors, image_sizes)
    anchors = model.anchor_generator(image_list, features)
    num_anchors_per_level = [f.size(2) * f.size(3) for f in features]

    return model.compute_loss(targets_batch, head_outputs, anchors, num_anchors_per_level)


def run_detect_training(
    *,
    org_id: str,
    backbone_key: str,
    s3_prefix: str,
    epochs: int,
    lr: float,
) -> dict:
    train_examples = load_split(org_id, s3_prefix, "train")
    eval_examples = load_split(org_id, s3_prefix, "eval")
    if not train_examples:
        raise ValueError(
            "No usable training examples (manifest empty, or every box was degenerate after "
            "clamping to the resized crop)."
        )

    model = build_fcos(backbone_key, num_classes=1)
    model.train()
    optimizer = torch.optim.AdamW(
        list(model.backbone.trainable_parameters()) + list(model.head.parameters()),
        lr=lr,
    )

    logger.info("dino_detect_train caching frozen features for %d examples", len(train_examples))
    cached = _cache_examples(model, train_examples)

    epoch_losses: list[float] = []
    for epoch in range(epochs):
        random.shuffle(cached)
        running_loss = 0.0
        num_batches = 0
        for i in range(0, len(cached), BATCH_SIZE):
            batch = cached[i : i + BATCH_SIZE]

            optimizer.zero_grad()
            losses = _train_step(model, batch)
            loss = sum(losses.values())
            loss.backward()
            optimizer.step()

            running_loss += float(loss.item())
            num_batches += 1

        avg_loss = running_loss / max(1, num_batches)
        epoch_losses.append(avg_loss)
        logger.info("dino_detect_train epoch=%d/%d avg_loss=%.4f", epoch + 1, epochs, avg_loss)

    # Artifact is pyramid + head state_dicts only — DINOv2's frozen params
    # are never re-saved, matching the whole point of this tier (many
    # per-class heads sharing one backbone download).
    artifact = {
        "kind": "fcos",
        "backbone_key": backbone_key,
        "pyramid_state_dict": model.backbone.pyramid.state_dict(),
        "head_state_dict": model.head.state_dict(),
    }
    buf = io.BytesIO()
    torch.save(artifact, buf)
    artifact_key = f"{s3_prefix}/head_artifact.pt"
    put_bytes(org_id, artifact_key, buf.getvalue(), "application/octet-stream")

    return {
        "artifact_key": artifact_key,
        "train_examples": len(train_examples),
        "eval_examples": len(eval_examples),
        "epoch_losses": epoch_losses,
        "final_loss": epoch_losses[-1] if epoch_losses else None,
    }
