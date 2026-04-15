### File: `app/workers/automation/scheduler.py`

"""
Manages Celery Beat schedule entries for automation pipelines with cron triggers.

When a pipeline with trigger_type='schedule' is activated, a dynamic Beat entry
is added. When paused/archived/deleted, the entry is removed.
"""
from app.workers.celery_app import celery_app


SCHEDULE_KEY_PREFIX = "automation-pipeline-"


def register_pipeline_schedule(pipeline_id: str, cron_expression: str, timezone: str = "UTC"):
    """Add a Celery Beat schedule entry for a pipeline."""
    from celery.schedules import crontab

    parts = cron_expression.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron_expression}")

    minute, hour, day_of_month, month_of_year, day_of_week = parts
    key = f"{SCHEDULE_KEY_PREFIX}{pipeline_id}"

    celery_app.conf.beat_schedule[key] = {
        "task": "app.workers.automation.tasks.trigger_scheduled_pipeline",
        "schedule": crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        ),
        "args": [pipeline_id],
        "options": {"queue": "automation"},
    }


def unregister_pipeline_schedule(pipeline_id: str):
    """Remove a Celery Beat schedule entry for a pipeline."""
    key = f"{SCHEDULE_KEY_PREFIX}{pipeline_id}"
    celery_app.conf.beat_schedule.pop(key, None)


def sync_scheduled_pipelines():
    """
    Query all active schedule-triggered pipelines from DB and register them
    in Celery Beat's in-memory schedule. Called on worker/beat startup via signal.

    Returns the number of pipelines registered.
    """
    from app.workers.db import WorkerSession
    from sqlalchemy import select, and_
    from app.models.automation import AutomationPipeline

    registered = 0
    try:
        with WorkerSession() as session:
            pipelines = session.execute(
                select(AutomationPipeline).where(
                    and_(
                        AutomationPipeline.trigger_type == "schedule",
                        AutomationPipeline.status == "active",
                        AutomationPipeline.deleted_at.is_(None),
                    )
                )
            ).scalars().all()

            for pipeline in pipelines:
                trigger_config = pipeline.trigger_config or {}
                cron_expr = trigger_config.get("cron_expression")
                tz = trigger_config.get("timezone", "UTC")
                if cron_expr:
                    try:
                        register_pipeline_schedule(str(pipeline.id), cron_expr, tz)
                        registered += 1
                    except ValueError:
                        # Invalid cron expression — skip silently
                        pass
    except Exception:
        # If DB access fails, silently skip — Beat will still work with empty schedule
        pass

    return registered

