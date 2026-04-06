from __future__ import annotations

import logging
import uuid

from app.core.enums import JobStatus
from app.models.job import Job
from app.services.model_manager import ModelManager
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import INFERENCE

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue=INFERENCE, max_retries=0)
def run_inference_batch(self, job_id: str):
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.error("inference_job_missing job_id=%s", job_id)
            return
        try:
            manager = ModelManager(session)
            manager.run_job(job)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            job = session.get(Job, uuid.UUID(job_id))
            if job is not None:
                job.status = JobStatus.FAILED
                job.logs = f"Inference execution failed: {exc}"
                session.commit()
            logger.exception("inference_job_failed job_id=%s error=%s", job_id, exc)
