"""
Naive in-process model runners.

The inference bodies here are COPIED VERBATIM from GreenMark/src/palm_api/app.py
(crown / yolo / sam3 platform handlers + helpers) so the naive baseline produces
byte-for-byte the same detections as the platform — only the orchestration differs.
We do NOT import app.py because importing it eagerly loads all three models.

Each runner takes a patch as a uint8 HxWx3 RGB ndarray and returns instances in
**patch-pixel space**; the caller (run_naive_benchmark) georeferences them.

Model weights live on shared NFS, visible from every SLURM node.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import torch
import torchvision

# ─── Weight paths (identical to palm_api) ────────────────────────────────────
_MODELS_DIR = os.environ.get("GREENMARK_MODELS", "/home/prass25/projects/GreenMark/models")
YOLO_PATH = os.path.join(_MODELS_DIR, "yolo11x-ortho.pt")
POSE_PATH = os.path.join(_MODELS_DIR, "yolov11x-pose.pt")
SAM3_PATH = os.path.join(_MODELS_DIR, "sam3.pt")
BPE_PATH = os.path.join(_MODELS_DIR, "bpe_simple_vocab_16e6.txt.gz")

# ─── Lazy singletons (load only the model a task needs) ──────────────────────
_yolo = None
_pose = None
_sam3 = None


def _get_yolo():
    global _yolo
    if _yolo is None:
        from ultralytics import YOLO
        _yolo = YOLO(YOLO_PATH)
    return _yolo


def _get_pose():
    global _pose
    if _pose is None:
        from ultralytics import YOLO
        _pose = YOLO(POSE_PATH)
    return _pose


def _get_sam3():
    global _sam3
    if _sam3 is None:
        from ultralytics.models.sam import SAM3SemanticPredictor
        overrides = dict(conf=0.25, task="segment", mode="predict",
                         model=SAM3_PATH, half=True, verbose=False)
        _sam3 = SAM3SemanticPredictor(overrides=overrides, bpe_path=BPE_PATH)
    return _sam3


# ─── Helpers (verbatim from app.py) ──────────────────────────────────────────
def apply_nms(detections: List[dict], iou_threshold: float = 0.5) -> List[dict]:
    if not detections:
        return []
    boxes = torch.tensor(
        [[d['center_x'] - d['width'] / 2, d['center_y'] - d['height'] / 2,
          d['center_x'] + d['width'] / 2, d['center_y'] + d['height'] / 2]
         for d in detections], dtype=torch.float32)
    scores = torch.tensor([d['confidence'] for d in detections], dtype=torch.float32)
    keep = torchvision.ops.nms(boxes, scores, iou_threshold)
    return [detections[i] for i in keep.numpy()]


def mask_to_polygons_with_holes(binary_mask: np.ndarray, min_area: float = 4.0):
    contours, hierarchy = cv2.findContours(binary_mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or hierarchy is None:
        return []
    hierarchy = hierarchy[0]
    children: Dict[int, List[int]] = {}
    for idx, h in enumerate(hierarchy):
        parent = int(h[3])
        if parent != -1:
            children.setdefault(parent, []).append(idx)

    def ring_of(idx: int):
        pts = contours[idx]
        if cv2.contourArea(pts) < min_area:
            return None
        pts = pts.squeeze(1)
        if pts.ndim != 2 or len(pts) < 3:
            return None
        return [[float(p[0]), float(p[1])] for p in pts]

    polygons = []
    for idx, h in enumerate(hierarchy):
        if int(h[3]) != -1:
            continue
        exterior = ring_of(idx)
        if exterior is None:
            continue
        rings = [exterior]
        for child in children.get(idx, []):
            hole = ring_of(child)
            if hole is not None:
                rings.append(hole)
        polygons.append(rings)
    return polygons


def _crown_center_from_keypoints(kpts, x1, y1, x2, y2):
    bbox_center = [float((x1 + x2) / 2), float((y1 + y2) / 2)]
    if kpts is None or len(kpts) == 0:
        return bbox_center
    best, best_conf = None, 0.0
    for kp in kpts:
        kx, ky, kc = float(kp[0]), float(kp[1]), float(kp[2])
        if kc > best_conf and (kx > 0 or ky > 0):
            best_conf, best = kc, [kx, ky]
    return best if best is not None else bbox_center


def _write_png(patch: np.ndarray) -> str:
    """Write an RGB patch to a temp PNG (mirrors palm_api's base64->tmp.png path)."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cv2.imwrite(path, cv2.cvtColor(patch, cv2.COLOR_RGB2BGR))
    return path


# ─── Runners — return instances in patch-pixel space ─────────────────────────
def run_yolo(patch: np.ndarray, conf: float = 0.25, iou: float = 0.7) -> List[dict]:
    model = _get_yolo()
    tmp = _write_png(patch)
    try:
        results = model.predict(tmp, save=False, imgsz=800, conf=conf, iou=iou, verbose=False)
        detections = []
        for result in results:
            if result.boxes is None or len(result.boxes.xyxy) == 0:
                continue
            for i, box in enumerate(result.boxes.xyxy):
                x1, y1, x2, y2 = map(float, box.tolist()[:4])
                detections.append({
                    "center_x": (x1 + x2) / 2, "center_y": (y1 + y2) / 2,
                    "width": x2 - x1, "height": y2 - y1,
                    "confidence": float(result.boxes.conf[i].item()),
                    "class_name": result.names[int(result.boxes.cls[i].item())],
                    "bbox_xyxy": [x1, y1, x2, y2],
                })
        instances = []
        for d in apply_nms(detections, iou):
            x1, y1, x2, y2 = d["bbox_xyxy"]
            instances.append({
                "label": d["class_name"], "score": d["confidence"],
                "polygon": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                "bbox": [x1, y1, x2 - x1, y2 - y1],
            })
        return instances
    finally:
        os.path.exists(tmp) and os.unlink(tmp)


def run_crown(patch: np.ndarray, conf: float = 0.25, iou: float = 0.7) -> List[dict]:
    model = _get_pose()
    tmp = _write_png(patch)
    try:
        results = model.predict(tmp, save=False, imgsz=800, conf=conf, iou=iou, verbose=False)
        detections = []
        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue
            boxes = result.boxes.xyxy.cpu().numpy()
            confs = result.boxes.conf.cpu().numpy()
            clss = result.boxes.cls.cpu().numpy()
            kpts_data = (result.keypoints.data.cpu().numpy()
                         if result.keypoints is not None else [])
            for i in range(len(boxes)):
                x1, y1, x2, y2 = [float(v) for v in boxes[i]]
                kpts = kpts_data[i] if i < len(kpts_data) else None
                detections.append({
                    "center_x": (x1 + x2) / 2, "center_y": (y1 + y2) / 2,
                    "width": x2 - x1, "height": y2 - y1,
                    "confidence": float(confs[i]),
                    "class_name": result.names[int(clss[i])],
                    "bbox_xyxy": [x1, y1, x2, y2],
                    "point": _crown_center_from_keypoints(kpts, x1, y1, x2, y2),
                })
        instances = []
        for d in apply_nms(detections, iou):
            x1, y1, x2, y2 = d["bbox_xyxy"]
            instances.append({
                "label": d["class_name"], "score": d["confidence"],
                "point": d["point"], "bbox": [x1, y1, x2 - x1, y2 - y1],
            })
        return instances
    finally:
        os.path.exists(tmp) and os.unlink(tmp)


def run_sam3(patch: np.ndarray, prompts: List[str]) -> List[dict]:
    predictor = _get_sam3()
    tmp = _write_png(patch)
    try:
        results = predictor(source=tmp, save=False, text=prompts)
        if not results:
            return []
        result = results[0]
        if result.boxes is None or result.masks is None:
            return []
        masks_data = (result.masks.data.cpu().numpy()
                      if result.masks.data is not None else None)
        oh, ow = int(result.orig_shape[0]), int(result.orig_shape[1])
        instances = []
        for i in range(len(result.boxes)):
            box = result.boxes[i]
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
            class_id = int(box.cls[0].item())
            label = prompts[class_id] if class_id < len(prompts) else prompts[0]
            score = float(box.conf[0].item()) if box.conf is not None else 1.0
            polygons = []
            if masks_data is not None and i < len(masks_data):
                binary = (masks_data[i] > 0.5).astype(np.uint8)
                if binary.shape != (oh, ow):
                    binary = cv2.resize(binary, (ow, oh), interpolation=cv2.INTER_NEAREST)
                polygons = mask_to_polygons_with_holes(binary)
            if not polygons:
                continue
            largest_ext = max((rings[0] for rings in polygons),
                              key=lambda r: cv2.contourArea(np.asarray(r, dtype=np.float32)),
                              default=[])
            instances.append({
                "label": label, "score": score, "polygons": polygons,
                "polygon": largest_ext, "bbox": [x1, y1, x2 - x1, y2 - y1],
            })
        return instances
    finally:
        os.path.exists(tmp) and os.unlink(tmp)


RUNNERS = {
    "/predict/yolo/platform": ("yolo", run_yolo),
    "/predict/crown/platform": ("crown", run_crown),
    "/segment/sam3/platform": ("sam3", run_sam3),
}
