"""Inference worker tasks — thin Celery wrapper over ``ModelManager``.

The real orchestration lives in :mod:`app.services.model_manager`. This file
just unpacks the ``job_id`` argument, opens a sync ``WorkerSession``, loads the
Job, and calls ``ModelManager.run_job``. All framework-specific behaviour is
configured on the ``ai_models`` row (``output_config.adapter`` etc.) — there is
no model-specific code here.
"""
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
def run_inference_batch(self, job_id: str) -> None:
    """Run a model-agnostic inference job.

    Delegates to :class:`app.services.model_manager.ModelManager`. On unhandled
    error, marks the Job as ``FAILED`` and retries up to ``max_retries`` times.
    """
    from app.core.enums import JobStatus
    from app.models.job import Job
    from app.services.model_manager import ModelManager

    job_uuid = uuid.UUID(job_id)
    automation_run_id: str | None = None
    automation_step_id: str | None = None
    output_data: dict | None = None

    with WorkerSession() as session:
        try:
            job = session.get(Job, job_uuid)
            if job is None:
                logger.warning("run_inference_batch: job %s not found", job_id)
                return

            cfg = job.config or {}
            automation_run_id = cfg.get("automation_run_id")
            automation_step_id = cfg.get("automation_step_id")

            manager = ModelManager(session)
            result = manager.run_job(job)

            annotation_set_ids = [str(s) for s in result.output_set_ids]
            output_data = {
                "annotation_set": {
                    "job_id": str(job.id),
                    "model_id": str(job.model_id) if job.model_id else None,
                    "model_name": job.model.name if getattr(job, "model", None) else None,
                    # Single canonical id for downstream nodes that take one set;
                    # the full list lets multi-set consumers (Overlay on Map,
                    # comparison/aggregate) iterate every per-item set.
                    "id": annotation_set_ids[0] if annotation_set_ids else None,
                    "annotation_set_ids": annotation_set_ids,
                    "processed_items": result.processed_items,
                    "failed_items": result.failed_items,
                }
            }

        except Exception as exc:
            logger.exception("run_inference_batch %s failed", job_id)
            with WorkerSession() as session2:
                job2 = session2.get(Job, job_uuid)
                if job2 is not None:
                    job2.status = JobStatus.FAILED
                    job2.logs = str(exc)[:2000]
                    job2.finished_at = _now()
                    session2.commit()
            raise self.retry(exc=exc)

    # Resume automation pipeline after the session closes so the worker that
    # picks up the follow-up step reads committed Job state.
    if automation_run_id and automation_step_id and output_data is not None:
        from app.workers.automation.tasks import resume_after_job  # noqa: PLC0415

        resume_after_job.delay(job_id, output_data)


# Legacy alias so callers that still import ``run_inference_job`` keep working
# until they're migrated. Remove once no imports remain.
run_inference_job = run_inference_batch
