"""
Dispatch automation pipeline runs in response to application events.
Provides both async (for API context) and sync (for Celery workers) versions.
"""
import logging
import uuid
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.automation import AutomationPipeline, AutomationRunStep
from app.schemas.automation import ReactFlowGraph
from app.automation.engine import get_root_node_ids

logger = logging.getLogger(__name__)


async def dispatch_event(session: AsyncSession, organization_id: str, event_type: str, event_data: dict) -> list[str]:
    """
    Find active pipelines with event triggers matching this event (async version).
    Creates all runs first, then commits once, then dispatches Celery tasks.
    Returns list of created run IDs.
    """
    from app.automation.engine import create_run

    result = await session.execute(
        select(AutomationPipeline).where(
            and_(
                AutomationPipeline.organization_id == uuid.UUID(organization_id),
                AutomationPipeline.trigger_type == "event",
                AutomationPipeline.status == "active",
                AutomationPipeline.deleted_at.is_(None),
            )
        )
    )
    pipelines = result.scalars().all()

    # Filter matching pipelines
    matched = []
    for pipeline in pipelines:
        trigger_config = pipeline.trigger_config or {}
        if trigger_config.get("event_type") != event_type:
            continue
        event_filters = trigger_config.get("event_filters", {})
        if event_filters and not all(event_data.get(k) == v for k, v in event_filters.items()):
            continue
        matched.append(pipeline)

    if not matched:
        return []

    # Create all runs in a single transaction
    runs = []
    for pipeline in matched:
        try:
            run = await create_run(session, pipeline, "event", event_data)
            runs.append((pipeline, run))
        except ValueError:
            logger.warning("Skipping pipeline %s: invalid graph", pipeline.id)
            continue

    if not runs:
        return []

    await session.flush()

    # Collect steps to dispatch before committing
    dispatch_list: list[tuple[str, str]] = []
    for pipeline, run in runs:
        graph = ReactFlowGraph(**pipeline.graph)
        root_ids = get_root_node_ids(graph)
        result = await session.execute(
            select(AutomationRunStep).where(
                and_(
                    AutomationRunStep.run_id == run.id,
                    AutomationRunStep.node_id.in_(root_ids),
                )
            )
        )
        for step in result.scalars().all():
            dispatch_list.append((str(run.id), str(step.id)))

    # Single commit for all runs
    await session.commit()

    # Dispatch Celery tasks after commit
    from app.workers.automation.tasks import execute_step
    for run_id, step_id in dispatch_list:
        execute_step.delay(run_id, step_id)

    return [str(run.id) for _, run in runs]


def dispatch_event_sync(session, organization_id: str, event_type: str, event_data: dict) -> list[str]:
    """
    Sync version for Celery worker context. Same logic, no await.
    """
    from app.automation.engine import create_run_sync

    pipelines = session.execute(
        select(AutomationPipeline).where(
            and_(
                AutomationPipeline.organization_id == uuid.UUID(organization_id),
                AutomationPipeline.trigger_type == "event",
                AutomationPipeline.status == "active",
                AutomationPipeline.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    matched = []
    for pipeline in pipelines:
        trigger_config = pipeline.trigger_config or {}
        if trigger_config.get("event_type") != event_type:
            continue
        event_filters = trigger_config.get("event_filters", {})
        if event_filters and not all(event_data.get(k) == v for k, v in event_filters.items()):
            continue
        matched.append(pipeline)

    if not matched:
        return []

    runs = []
    for pipeline in matched:
        try:
            run = create_run_sync(session, pipeline, "event", event_data)
            runs.append((pipeline, run))
        except ValueError:
            logger.warning("Skipping pipeline %s: invalid graph", pipeline.id)
            continue

    if not runs:
        return []

    # Single commit
    session.commit()

    # Dispatch after commit
    from app.workers.automation.tasks import execute_step
    run_ids = []
    for pipeline, run in runs:
        graph = ReactFlowGraph(**pipeline.graph)
        root_ids = get_root_node_ids(graph)
        steps = session.execute(
            select(AutomationRunStep).where(
                and_(
                    AutomationRunStep.run_id == run.id,
                    AutomationRunStep.node_id.in_(root_ids),
                )
            )
        ).scalars().all()

        for step in steps:
            execute_step.delay(str(run.id), str(step.id))

        run_ids.append(str(run.id))

    return run_ids
