"""geoops-service — GPU model-training/serving microservice.

Serves as the single generic backend for every backbone+head combination
AwakeForestProd's geoops.* CPU orchestration calls out to (DINOv2 today,
YOLO/others later) — one set of routes, config-driven per request, rather
than a service per backbone. /train (detect/FCOS only so far) is real;
/infer and /eval are still stubs.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

import torch
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from app.auth import require_bearer_token
from app.backbones import BACKBONES

# PyTorch defaults to intra-op parallelism across every available core. That's
# right for one big batch on a beefy machine, wrong for this tier's actual
# workload: tiny per-class heads, batches of 4, on hosts that can have dozens
# of cores. Coordinating that many threads per matmul costs more than the
# matmul itself — observed directly: a training run pegged 51 of 64 cores
# (5148% CPU) while making barely any progress. Cap it once, at process
# startup, before any tensor op runs.
torch.set_num_threads(min(8, os.cpu_count() or 8))

logger = logging.getLogger(__name__)

app = FastAPI(title="geoops-service")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/backbones", dependencies=[Depends(require_bearer_token)])
async def list_backbones() -> dict[str, dict]:
    return BACKBONES


class TrainRequest(BaseModel):
    org_id: str
    backbone_key: str = "dinov2-vits14"
    kind: Literal["fcos", "classify"] = "fcos"
    s3_prefix: str  # e.g. "training-runs/{job_id}" — same layout dino_tasks.py exports to
    epochs: int = 20
    lr: float = 1e-3


class InferRequest(BaseModel):
    """Matches app.services.model_manager.ModelManager._call_model's generic
    body exactly (dataset_item_id, georef_metadata, patch_image_base64, ...)
    plus fields injected via this head's ai_models.request_config.payload
    (org_id, kind, backbone_key, artifact_key, class_label, score_thresh) —
    that's how a per-class head's identity reaches this endpoint without
    ModelManager needing any DINOv2-specific code.
    """

    model_config = {"extra": "allow"}  # ModelManager sends more fields than we read

    georef_metadata: dict[str, Any]
    patch_image_base64: str
    org_id: str
    artifact_key: str
    class_label: str
    kind: Literal["fcos", "classify"] = "fcos"
    backbone_key: str = "dinov2-vits14"
    score_thresh: float = 0.3


class EvalRequest(BaseModel):
    org_id: str
    s3_prefix: str  # manifest.json lives here — same layout /train reads from
    artifact_key: str | None = None  # defaults to f"{s3_prefix}/head_artifact.pt"
    kind: Literal["fcos", "classify"] = "fcos"
    split: Literal["train", "eval", "test"] = "test"
    iou_thresh: float = 0.5
    score_thresh: float = 0.5


@app.post("/train", dependencies=[Depends(require_bearer_token)])
async def train(payload: TrainRequest) -> dict:
    try:
        if payload.kind == "classify":
            from app.classify_train import run_classify_training  # noqa: PLC0415

            return run_classify_training(
                org_id=payload.org_id,
                backbone_key=payload.backbone_key,
                s3_prefix=payload.s3_prefix,
                epochs=payload.epochs,
                lr=payload.lr,
            )
        from app.train import run_detect_training  # noqa: PLC0415 — lazy: keeps torch import off /health's path

        return run_detect_training(
            org_id=payload.org_id,
            backbone_key=payload.backbone_key,
            s3_prefix=payload.s3_prefix,
            epochs=payload.epochs,
            lr=payload.lr,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception:
        logger.exception("train_failed org_id=%s s3_prefix=%s", payload.org_id, payload.s3_prefix)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Training failed") from None


@app.post("/infer", dependencies=[Depends(require_bearer_token)])
async def infer(payload: InferRequest) -> dict:
    try:
        if payload.kind == "classify":
            from app.classify_infer import run_classify_inference  # noqa: PLC0415
            from app.load_model import load_trained_classify_model  # noqa: PLC0415

            model = load_trained_classify_model(payload.org_id, payload.artifact_key)
            predictions = run_classify_inference(
                model,
                payload.patch_image_base64,
                payload.georef_metadata,
                class_label=payload.class_label,
                score_thresh=payload.score_thresh,
            )
        else:
            from app.infer_model import run_detect_inference  # noqa: PLC0415
            from app.load_model import load_trained_detect_model  # noqa: PLC0415

            model = load_trained_detect_model(payload.org_id, payload.artifact_key)
            predictions = run_detect_inference(
                model,
                payload.patch_image_base64,
                payload.georef_metadata,
                class_label=payload.class_label,
                score_thresh=payload.score_thresh,
            )
        return {"predictions": predictions}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception:
        logger.exception("infer_failed org_id=%s artifact_key=%s", payload.org_id, payload.artifact_key)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Inference failed") from None


@app.post("/eval", dependencies=[Depends(require_bearer_token)])
async def eval_head(payload: EvalRequest) -> dict:
    artifact_key = payload.artifact_key or f"{payload.s3_prefix}/head_artifact.pt"
    try:
        if payload.kind == "classify":
            from app.classify_dataset import load_classify_split  # noqa: PLC0415
            from app.classify_eval import evaluate_classify  # noqa: PLC0415
            from app.load_model import load_trained_classify_model  # noqa: PLC0415

            model = load_trained_classify_model(payload.org_id, artifact_key)
            examples = load_classify_split(payload.org_id, payload.s3_prefix, payload.split)
            return evaluate_classify(model, examples, score_thresh=payload.score_thresh)

        from app.dataset import load_split  # noqa: PLC0415
        from app.eval_model import evaluate_detect  # noqa: PLC0415
        from app.load_model import load_trained_detect_model  # noqa: PLC0415

        model = load_trained_detect_model(payload.org_id, artifact_key)
        examples = load_split(payload.org_id, payload.s3_prefix, payload.split)
        return evaluate_detect(model, examples, iou_thresh=payload.iou_thresh)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception:
        logger.exception(
            "eval_failed org_id=%s s3_prefix=%s artifact_key=%s", payload.org_id, payload.s3_prefix, artifact_key
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Eval failed") from None
