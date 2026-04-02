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


