"""Inference worker tasks (ML model inference)."""
import logging
import uuid
from datetime import UTC, datetime

from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import INFERENCE

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, queue=INFERENCE, max_retries=2, default_retry_delay=30)
def run_inference_job(self, job_id: str) -> None:
    """
    Run ML inference for a Job. Creates an AnnotationSet with predictions,
    then calls resume_after_job to continue the automation pipeline.

    Called by execute_run_inference node via DeferToJob pattern.
    """
    with WorkerSession() as session:
        from app.models.job import Job
        from app.models.annotation_set import AnnotationSet

        try:
            job = session.get(Job, uuid.UUID(job_id))
            if not job:
                logger.warning("run_inference_job: job %s not found", job_id)
                return

            job.status = "running"
            job.started_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

            # Load config
            cfg = job.config or {}
            model_id = cfg.get("model_id")
            item_ids = cfg.get("item_ids", [])
            confidence_threshold = cfg.get("confidence_threshold", 0.5)

            if not model_id or not item_ids:
                raise ValueError("model_id and item_ids required in job.config")

            # Create output annotation set to hold predictions
            # In a real implementation, this would run the actual ML model
            # For now, it's a placeholder that creates an empty set
            output_set = AnnotationSet(
                name=f"Inference Results ({job_id[:8]})",
                created_by_job_id=job.id,
            )
            session.add(output_set)
            session.flush()

            # TODO: In production, run the actual ML inference here
            # For each item_id in item_ids:
            #   1. Load raster data from S3 or STAC
            #   2. Run model inference
            #   3. Post-process predictions
            #   4. Create Annotation rows in output_set for each detection
            # For now, we just create the empty set and mark as completed.

            job.status = "completed"
            job.progress = 1.0
            job.finished_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

            # Resume the automation pipeline step that was waiting on this job
            automation_run_id = cfg.get("automation_run_id")
            automation_step_id = cfg.get("automation_step_id")

            if automation_run_id and automation_step_id:
                from app.workers.automation.tasks import resume_after_job

                output_data = {
                    "predictions": {
                        "job_id": str(job.id),
                        "annotation_set_id": str(output_set.id),
                        "item_count": len(item_ids),
                        "confidence_threshold": confidence_threshold,
                    }
                }
                resume_after_job.delay(job_id, output_data)

        except Exception as exc:
            logger.exception("run_inference_job %s failed", job_id)
            with WorkerSession() as session2:
                job = session2.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "failed"
                    job.logs = str(exc)[:2000]
                    job.finished_at = datetime.now(UTC).replace(tzinfo=None)
                    session2.commit()
            raise self.retry(exc=exc)
