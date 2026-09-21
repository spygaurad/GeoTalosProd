"""Classifier evaluation: accuracy/precision/recall/F1 at a fixed score
threshold over a held-out split. Much simpler than detect's AP50 — no
localization to score, just "did it call this patch right."
"""
from __future__ import annotations

import torch

from app.classify_dataset import ClassifyExample


def evaluate_classify(model, examples: list[ClassifyExample], score_thresh: float = 0.5) -> dict:
    model.eval()
    tp = fp = tn = fn = 0
    with torch.no_grad():
        for ex in examples:
            logit = model(ex.image.unsqueeze(0))[0]
            score = torch.sigmoid(logit).item()
            pred_positive = score >= score_thresh
            actual_positive = ex.label > 0.5
            if pred_positive and actual_positive:
                tp += 1
            elif pred_positive and not actual_positive:
                fp += 1
            elif not pred_positive and not actual_positive:
                tn += 1
            else:
                fn += 1

    total = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )
    accuracy = (tp + tn) / total if total > 0 else None

    return {
        "num_images": total,
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "score_thresh": score_thresh,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }
