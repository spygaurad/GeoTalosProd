import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready, beat_init

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "awakeforest",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.ingestion.tasks",
        "app.workers.analysis.tasks",
        "app.workers.automation.tasks",
        "app.workers.inference.tasks",
        "app.workers.bulk.tasks",
        "app.workers.default.tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Route tasks to the correct queues based on their module
    task_routes={
        "app.workers.ingestion.tasks.*": {"queue": "ingestion"},
        "app.workers.bulk.tasks.*": {"queue": "bulk"},
        "app.workers.analysis.tasks.*": {"queue": "analysis"},
        "app.workers.automation.tasks.*": {"queue": "automation"},
        "app.workers.inference.tasks.*": {"queue": "inference"},
        "app.workers.default.tasks.*": {"queue": "default"},

    },
    # Beat schedule
    beat_schedule={
        "refresh-annotation-statistics-hourly": {
            "task": "app.workers.ingestion.tasks.refresh_annotation_statistics",
            "schedule": crontab(minute=0),  # every hour at :00
            "options": {"queue": "default"},
        },
        "cleanup-stale-pending-jobs-hourly": {
            "task": "app.workers.ingestion.tasks.cleanup_stale_pending_jobs",
            "schedule": crontab(minute=30),  # every hour at :30
            "options": {"queue": "default"},
        },
        "cleanup-stale-running-jobs-every-2min": {
            "task": "app.workers.ingestion.tasks.cleanup_stale_running_jobs",
            "schedule": crontab(minute="*/2"),  # every 2 minutes
            "options": {"queue": "default"},
        },
    },
)


@worker_ready.connect
def _reset_orphaned_running_jobs(sender, **kwargs):
    """On worker startup, mark any jobs stuck in 'running' as failed.

    Jobs stay in 'running' state in the DB when a worker restarts mid-task
    because the Celery task is lost but the DB row isn't updated.
    Only affects jobs that started more than 10 minutes ago to avoid racing
    with a legitimately in-progress task on another worker instance.
    """
    from sqlalchemy import text
    from app.workers.db import WorkerSession

    try:
        with WorkerSession() as session:
            result = session.execute(
                text("""
                    UPDATE jobs
                    SET status = 'failed',
                        logs = COALESCE(logs || E'\\n', '') ||
                               'Job orphaned: worker process restarted while task was running.',
                        finished_at = NOW()
                    WHERE status = 'running'
                      AND started_at < NOW() - INTERVAL '10 minutes'
                    RETURNING id
                """)
            )
            count = result.rowcount
            session.commit()
            if count:
                logger.warning("worker_startup_cleanup reset %d orphaned running jobs", count)
    except Exception:
        logger.exception("worker_startup_cleanup failed — orphaned jobs not reset")


@beat_init.connect
def _sync_scheduled_pipelines_on_beat_start(sender, **kwargs):
    """On Celery Beat startup, reload all active schedule-triggered pipelines from DB.

    This ensures scheduled pipelines persist across Beat process restarts, even though
    changes to in-memory beat_schedule are lost on restart. The API path still provides
    immediate effect without restart.
    """
    try:
        from app.workers.automation.scheduler import sync_scheduled_pipelines
        count = sync_scheduled_pipelines()
        logger.info("beat_startup: registered %d scheduled automation pipeline(s)", count)
    except Exception:
        logger.exception("beat_startup: failed to sync scheduled pipelines")
