"""Detection evaluation: single-class AP@0.5 (PASCAL VOC-style, 101-point
interpolated precision-recall), plus precision/recall/mean-IoU at the raw
prediction set for interpretability.

Single-class because this tier trains one head per class — there's no
multi-class mAP to average over.
"""
from __future__ import annotations

import numpy as np
import torch

from app.dataset import Example


def _iou(box_a: list[float], box_b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def evaluate_detect(model: torch.nn.Module, examples: list[Example], iou_thresh: float = 0.5) -> dict:
    model.eval()
    all_preds: list[tuple[int, list[float], float]] = []  # (image_idx, box, score)
    all_gts: list[tuple[int, list[float]]] = []  # (image_idx, box)

    with torch.no_grad():
        for img_idx, ex in enumerate(examples):
            pred = model([ex.image])[0]
            for box, score in zip(pred["boxes"].tolist(), pred["scores"].tolist(), strict=True):
                all_preds.append((img_idx, box, score))
            for box in ex.boxes.tolist():
                all_gts.append((img_idx, box))

    base = {"num_images": len(examples), "num_ground_truth": len(all_gts), "num_predictions": len(all_preds)}
    if not all_gts:
        return {**base, "ap50": None, "note": "No ground-truth boxes in this split."}
    if not all_preds:
        return {**base, "ap50": 0.0, "precision": 0.0, "recall": 0.0, "mean_iou_tp": None}

    preds_sorted = sorted(all_preds, key=lambda p: -p[2])
    gt_by_image: dict[int, list[dict]] = {}
    for img_idx, box in all_gts:
        gt_by_image.setdefault(img_idx, []).append({"box": box, "matched": False})

    n_preds = len(preds_sorted)
    tp = np.zeros(n_preds)
    fp = np.zeros(n_preds)
    matched_ious: list[float] = []
    for i, (img_idx, box, _score) in enumerate(preds_sorted):
        gts = gt_by_image.get(img_idx, [])
        best_iou, best_j = 0.0, -1
        for j, g in enumerate(gts):
            if g["matched"]:
                continue
            iou = _iou(box, g["box"])
            if iou > best_iou:
                best_iou, best_j = iou, j
        if best_iou >= iou_thresh and best_j >= 0:
            gts[best_j]["matched"] = True
            tp[i] = 1.0
            matched_ious.append(best_iou)
        else:
            fp[i] = 1.0

    tp_cum = np.cumsum(tp)
    fp_cum = np.cumsum(fp)
    n_gt = len(all_gts)
    recall = tp_cum / n_gt
    precision = tp_cum / np.maximum(tp_cum + fp_cum, 1e-9)

    # 101-point interpolated AP (COCO-style continuous integration).
    ap = 0.0
    for t in np.linspace(0, 1, 101):
        mask = recall >= t
        p = precision[mask].max() if mask.any() else 0.0
        ap += float(p) / 101

    return {
        **base,
        "ap50": round(ap, 4),
        "precision": round(float(precision[-1]), 4),
        "recall": round(float(recall[-1]), 4),
        "mean_iou_tp": round(float(np.mean(matched_ious)), 4) if matched_ious else None,
        "iou_thresh": iou_thresh,
    }
