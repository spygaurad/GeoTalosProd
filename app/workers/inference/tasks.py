"""Inference worker tasks (ML model inference via SAM3)."""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import INFERENCE

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@celery_app.task(bind=True, queue=INFERENCE, max_retries=2, default_retry_delay=30)
def run_inference_job(self, job_id: str) -> None:
    """Run an inference job.

    Two code paths based on job.config:

    1. Direct path (new): cfg has ``annotation_set_id`` — inference triggered via
       ``POST /inference/sam3``. Loads the pre-created AnnotationSet and calls
       ``run_sam3_inference`` for a single dataset_item.

    2. Automation path (existing): cfg has ``item_ids`` (no annotation_set_id) —
       inference triggered by the automation pipeline's ``run_inference`` node.
       Creates one AnnotationSet per item and loops.

    On completion, ``resume_after_job`` is invoked if the automation pipeline
    context is present (automation_run_id + automation_step_id in cfg).
    """
    from app.models.ai_model import AIModel
    from app.models.annotation_set import AnnotationSet
    from app.models.dataset_item import DatasetItem
    from app.models.job import Job
    from app.workers.inference.sam3_runner import run_sam3_inference

    job_uuid = uuid.UUID(job_id)

    with WorkerSession() as session:
        try:
            job = session.get(Job, job_uuid)
            if not job:
                logger.warning("run_inference_job: job %s not found", job_id)
                return

            job.status = "running"
            job.started_at = _now()
            session.commit()

            cfg = job.config or {}
            model_id = cfg.get("model_id")
            if not model_id:
                raise ValueError("model_id required in job.config")

            ai_model = session.get(AIModel, uuid.UUID(model_id))
            if not ai_model:
                raise ValueError(f"AIModel {model_id} not found")

            warnings_all: list[str] = []

            if cfg.get("annotation_set_id"):
                # ── Direct path ──────────────────────────────────────────
                aset_id = uuid.UUID(cfg["annotation_set_id"])
                item_id = uuid.UUID(cfg["dataset_item_id"])

                annotation_set = session.get(AnnotationSet, aset_id)
                if not annotation_set:
                    raise ValueError(f"AnnotationSet {aset_id} not found")
                dataset_item = session.get(DatasetItem, item_id)
                if not dataset_item:
                    raise ValueError(f"DatasetItem {item_id} not found")

                job.total_items = 1
                summary = run_sam3_inference(
                    session, job, annotation_set, ai_model, dataset_item, cfg
                )
                warnings_all.extend(summary.get("warnings", []))
                job.processed_items = 1
                output_data = {
                    "predictions": {
                        "job_id": str(job.id),
                        "annotation_set_id": str(annotation_set.id),
                        "item_count": 1,
                        **{k: v for k, v in summary.items() if k != "warnings"},
                    }
                }
            else:
                # ── Automation path ──────────────────────────────────────
                item_ids = cfg.get("item_ids") or []
                if not item_ids:
                    raise ValueError("item_ids required in job.config (automation path)")

                job.total_items = len(item_ids)
                session.commit()

                last_aset_id: uuid.UUID | None = None
                for idx, raw_item_id in enumerate(item_ids):
                    item_id = uuid.UUID(raw_item_id)
                    dataset_item = session.get(DatasetItem, item_id)
                    if not dataset_item:
                        job.failed_items += 1
                        warnings_all.append(f"dataset_item {raw_item_id} not found")
                        continue

                    aset = AnnotationSet(
                        organization_id=dataset_item.organization_id,
                        name=f"Inference {ai_model.name} ({str(job.id)[:8]}) #{idx + 1}",
                        source_type="model",
                        model_id=ai_model.id,
                        job_id=job.id,
                        schema_id=ai_model.annotation_schema_id,
                        dataset_item_id=dataset_item.id,
                        dataset_id=dataset_item.dataset_id,
                    )
                    session.add(aset)
                    session.flush()
                    last_aset_id = aset.id

                    # Use model.config.default_prompt if available
                    item_cfg = {
                        **cfg,
                        "task_type": cfg.get("task_type")
                            or (ai_model.config or {}).get("default_task_type", "pcs"),
                        "output_format": cfg.get("output_format", "vector"),
                        "prompt_pcs": cfg.get("prompt_pcs")
                            or (ai_model.config or {}).get("default_prompt_pcs"),
                        "prompt_pvs": cfg.get("prompt_pvs")
                            or (ai_model.config or {}).get("default_prompt_pvs"),
                    }

                    try:
                        summary = run_sam3_inference(
                            session, job, aset, ai_model, dataset_item, item_cfg
                        )
                        warnings_all.extend(summary.get("warnings", []))
                    except Exception as per_item_exc:
                        logger.exception(
                            "inference_item_failed job_id=%s item_id=%s", job_id, raw_item_id
                        )
                        job.failed_items += 1
                        warnings_all.append(f"item {raw_item_id} failed: {per_item_exc}")

                    job.processed_items = idx + 1
                    job.progress = job.processed_items / job.total_items
                    session.commit()

                output_data = {
                    "predictions": {
                        "job_id": str(job.id),
                        "annotation_set_id": str(last_aset_id) if last_aset_id else None,
                        "item_count": len(item_ids),
                        "failed_items": job.failed_items,
                    }
                }

            job.status = "completed"
            job.progress = 1.0
            job.finished_at = _now()
            if warnings_all:
                job.logs = "\n".join(warnings_all)[:2000]
            session.commit()

            # Resume automation pipeline if we were called from one
            automation_run_id = cfg.get("automation_run_id")
            automation_step_id = cfg.get("automation_step_id")
            if automation_run_id and automation_step_id:
                from app.workers.automation.tasks import resume_after_job
                resume_after_job.delay(job_id, output_data)

        except Exception as exc:
            logger.exception("run_inference_job %s failed", job_id)
            with WorkerSession() as session2:
                job2 = session2.get(Job, job_uuid)
                if job2:
                    job2.status = "failed"
                    job2.logs = str(exc)[:2000]
                    job2.finished_at = _now()
                    session2.commit()
            raise self.retry(exc=exc)
