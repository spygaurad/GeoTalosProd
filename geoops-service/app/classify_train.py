"""Classifier training — same feature-caching principle as the detect
tier's train.py: DINOv2's CLS token for a given image is identical every
epoch (frozen), so it's extracted once per image up front, then every
epoch only re-runs the actual trainable part (one linear layer) against
the cached tokens. Even more valuable here than for detect: the classifier
forward pass on cached features is essentially free, so an entire training
run is dominated by the one-time caching pass.
"""
from __future__ import annotations

import io
import logging
import random

import torch

from app.classify_dataset import load_classify_split
from app.classify_model import build_classifier
from app.storage import put_bytes

logger = logging.getLogger(__name__)

BATCH_SIZE = 8


def run_classify_training(
    *,
    org_id: str,
    backbone_key: str,
    s3_prefix: str,
    epochs: int,
    lr: float,
) -> dict:
    train_examples = load_classify_split(org_id, s3_prefix, "train")
    eval_examples = load_classify_split(org_id, s3_prefix, "eval")
    if not train_examples:
        raise ValueError("No usable training examples (manifest empty).")
    if not any(e.label > 0.5 for e in train_examples) or not any(e.label < 0.5 for e in train_examples):
        raise ValueError("Training split needs both positive and negative examples.")

    model = build_classifier(backbone_key)
    model.train()
    optimizer = torch.optim.AdamW(model.linear.parameters(), lr=lr)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    logger.info("dino_classify_train caching frozen features for %d examples", len(train_examples))
    cached: list[dict] = []
    with torch.no_grad():
        for ex in train_examples:
            cls_token = model.extract_cls(ex.image.unsqueeze(0))[0]
            cached.append({"cls": cls_token, "label": torch.tensor(ex.label, dtype=torch.float32)})

    epoch_losses: list[float] = []
    for epoch in range(epochs):
        random.shuffle(cached)
        running_loss = 0.0
        num_batches = 0
        for i in range(0, len(cached), BATCH_SIZE):
            batch = cached[i : i + BATCH_SIZE]
            cls_batch = torch.stack([b["cls"] for b in batch])
            labels_batch = torch.stack([b["label"] for b in batch])

            optimizer.zero_grad()
            logits = model.linear(cls_batch).squeeze(-1)
            loss = loss_fn(logits, labels_batch)
            loss.backward()
            optimizer.step()

            running_loss += float(loss.item())
            num_batches += 1

        avg_loss = running_loss / max(1, num_batches)
        epoch_losses.append(avg_loss)
        logger.info("dino_classify_train epoch=%d/%d avg_loss=%.4f", epoch + 1, epochs, avg_loss)

    # Artifact is just the linear layer — a handful of KB, not DINOv2's
    # frozen params. Even smaller than the detect tier's head.
    artifact = {
        "kind": "classify",
        "backbone_key": backbone_key,
        "linear_state_dict": model.linear.state_dict(),
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
