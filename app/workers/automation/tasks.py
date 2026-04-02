"""
Automation pipeline Celery tasks.

Two task types:
1. execute_step — generic dispatcher that runs any node type
2. trigger_scheduled_pipeline — called by Celery Beat for scheduled pipelines
"""
import logging
import uuid
from datetime import datetime, UTC

from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession

logger = logging.getLogger(__name__)

AUTOMATION_QUEUE = "automation"


def _publish_step_event(org_id, event_type: str, run, step, **extra):
    """Publish an automation step event via the event bus (fire-and-forget)."""
    try:
        from app.core.events import publish_sync
        publish_sync(str(org_id), event_type, {
            "run_id": str(run.id),
            "step_id": str(step.id),
            "node_id": step.node_id,
            "node_type": step.node_type,
            "status": step.status,
            "progress": run.progress,
            **extra,
        })
    except Exception:
        logger.debug("Failed to publish %s event", event_type, exc_info=True)


def _publish_run_event(org_id, event_type: str, run):
    """Publish an automation run event via the event bus (fire-and-forget)."""
    try:
        from app.core.events import publish_sync
        publish_sync(str(org_id), event_type, {
            "run_id": str(run.id),
            "pipeline_id": str(run.pipeline_id),
            "status": run.status,
            "progress": run.progress,
            "completed_steps": run.completed_steps,
            "failed_steps": run.failed_steps,
            "total_steps": run.total_steps,
        })
    except Exception:
        logger.debug("Failed to publish %s event", event_type, exc_info=True)


@celery_app.task(bind=True, queue=AUTOMATION_QUEUE, max_retries=0)
def execute_step(self, run_id: str, step_id: str) -> None:
    """
    Execute a single automation step.

    1. Load step row, mark as running.
    2. Resolve input_data from upstream completed steps.
    3. Call the node type's execute function.
    4. Write output_data.
    5. Check for ready downstream steps and enqueue them.
    6. If all steps done, finalize the run.
    """
    with WorkerSession() as session:
        from app.models.automation import AutomationRun, AutomationRunStep
        from app.automation.registry import get_node_type
        from app.automation.engine import (
            get_downstream_node_ids,
            get_upstream_edges,
            resolve_step_inputs,
        )
        from app.schemas.automation import ReactFlowGraph

        step = session.get(AutomationRunStep, uuid.UUID(step_id))
        if not step or step.status != "pending":
            return

        run = session.get(AutomationRun, uuid.UUID(run_id))
        if not run or run.status == "cancelled":
            return

        # Mark run as running on first step
        if run.status == "pending":
            run.status = "running"
            run.started_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

        # Mark step as running
        step.status = "running"
        step.started_at = datetime.now(UTC).replace(tzinfo=None)
        step.celery_task_id = self.request.id
        session.commit()

        _publish_step_event(run.organization_id, "automation.step.started", run, step)

        graph = ReactFlowGraph(**run.graph_snapshot)

        try:
            # Resolve inputs from upstream steps
            all_steps = session.query(AutomationRunStep).filter_by(run_id=run.id).all()
            completed_map = {s.node_id: s for s in all_steps if s.status == "completed"}
            input_data = resolve_step_inputs(graph, step.node_id, completed_map)
            step.input_data = input_data

            # Get the node type executor (direct function reference from decorator)
            node_type = get_node_type(step.node_type)
            if not node_type or not node_type.executor:
                raise ValueError(f"No executor for node type: {step.node_type}")

            # Execute — returns output_data dict or DeferToJob
            from app.automation.registry import DeferToJob

            result = node_type.executor(
                session=session,
                config=step.config or {},
                input_data=input_data,
                organization_id=str(run.organization_id),
                run_id=str(run.id),
                step_id=str(step.id),
                trigger_data=run.trigger_data,
            )

            if isinstance(result, DeferToJob):
                # Step delegates to a long-running Job — park it
                step.status = "waiting_for_job"
                step.waiting_for_job_id = uuid.UUID(result.job_id)
                session.commit()
                return

            # Mark complete
            step.status = "completed"
            step.output_data = result or {}
            step.completed_at = datetime.now(UTC).replace(tzinfo=None)
            if step.started_at:
                step.duration_ms = int(
                    (step.completed_at - step.started_at).total_seconds() * 1000
                )
            run.completed_steps += 1
            run.progress = run.completed_steps / run.total_steps if run.total_steps > 0 else 0
            session.commit()

            _publish_step_event(run.organization_id, "automation.step.completed", run, step)

        except Exception as exc:
            logger.exception("Step %s failed: %s", step_id, exc)
            step.status = "failed"
            step.error = str(exc)[:2000]
            step.completed_at = datetime.now(UTC).replace(tzinfo=None)
            if step.started_at:
                step.duration_ms = int(
                    (step.completed_at - step.started_at).total_seconds() * 1000
                )
            run.failed_steps += 1
            session.commit()

            _publish_step_event(
                run.organization_id, "automation.step.failed", run, step,
                error=step.error,
            )

            # Skip all downstream steps
            _skip_downstream(session, run, graph, step.node_id)

            # Check if run is done
            _check_run_completion(session, run)
            return

        # Dispatch ready downstream steps
        _dispatch_ready_downstream(session, run, graph, step.node_id)

        # Check if run is done
        _check_run_completion(session, run)


def _skip_downstream(session, run, graph, failed_node_id: str):
    """Mark all downstream steps as skipped when a step fails."""
    from collections import deque
    from app.automation.engine import get_downstream_node_ids
    from app.models.automation import AutomationRunStep

    to_skip = set()
    queue = deque(get_downstream_node_ids(graph, failed_node_id))
    while queue:
        nid = queue.popleft()
        if nid not in to_skip:
            to_skip.add(nid)
            queue.extend(get_downstream_node_ids(graph, nid))

    if to_skip:
        session.query(AutomationRunStep).filter(
            AutomationRunStep.run_id == run.id,
            AutomationRunStep.node_id.in_(to_skip),
            AutomationRunStep.status == "pending",
        ).update({"status": "skipped"}, synchronize_session="fetch")
        session.commit()


def _dispatch_ready_downstream(session, run, graph, completed_node_id: str):
    """Check downstream nodes and dispatch those with all inputs resolved."""
    from app.automation.engine import get_downstream_node_ids, get_upstream_edges
    from app.models.automation import AutomationRunStep

    downstream_ids = get_downstream_node_ids(graph, completed_node_id)
    all_steps = {s.node_id: s for s in session.query(AutomationRunStep).filter_by(run_id=run.id).all()}

    for nid in downstream_ids:
        downstream_step = all_steps.get(nid)
        if not downstream_step or downstream_step.status != "pending":
            continue

        # Check if ALL upstream nodes are completed
        upstream_edges = get_upstream_edges(graph, nid)
        upstream_node_ids = {e.source for e in upstream_edges}
        all_ready = all(
            all_steps.get(uid) and all_steps[uid].status == "completed"
            for uid in upstream_node_ids
        )
        if all_ready:
            execute_step.delay(str(run.id), str(downstream_step.id))


def _check_run_completion(session, run):
    """Check if all steps are terminal (completed/failed/skipped) and finalize run."""
    from app.models.automation import AutomationRunStep, AutomationPipeline

    pending_count = session.query(AutomationRunStep).filter(
        AutomationRunStep.run_id == run.id,
        AutomationRunStep.status.in_(["pending", "running", "waiting_for_job"]),
    ).count()

    if pending_count == 0:
        run.status = "completed" if run.failed_steps == 0 else "failed"
        run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        run.progress = 1.0

        # Update pipeline last_run_status
        pipeline = session.get(AutomationPipeline, run.pipeline_id)
        if pipeline:
            pipeline.last_run_status = run.status

        session.commit()

        event_type = "automation.run.completed" if run.status == "completed" else "automation.run.failed"
        _publish_run_event(run.organization_id, event_type, run)

@celery_app.task(queue=AUTOMATION_QUEUE)
def resume_after_job(job_id: str, output_data: dict | None = None) -> None:
    """Called when a long-running Job completes. Resumes the waiting automation step.

    Hook this into your Job completion logic:
        from app.workers.automation.tasks import resume_after_job
        resume_after_job.delay(str(job.id), {"predictions": {...}})
    """
    with WorkerSession() as session:
        from app.models.automation import AutomationRun, AutomationRunStep
        from app.automation.engine import get_downstream_node_ids
        from app.schemas.automation import ReactFlowGraph

        step = session.query(AutomationRunStep).filter(
            AutomationRunStep.waiting_for_job_id == uuid.UUID(job_id),
            AutomationRunStep.status == "waiting_for_job",
        ).first()
        if not step:
            logger.warning("resume_after_job: no waiting step for job %s", job_id)
            return

        run = session.get(AutomationRun, step.run_id)
        if not run or run.status == "cancelled":
            return

        step.status = "completed"
        step.output_data = output_data or {}
        step.completed_at = datetime.now(UTC).replace(tzinfo=None)
        step.waiting_for_job_id = None
        if step.started_at:
            step.duration_ms = int(
                (step.completed_at - step.started_at).total_seconds() * 1000
            )
        run.completed_steps += 1
        run.progress = run.completed_steps / run.total_steps if run.total_steps > 0 else 0
        session.commit()

        _publish_step_event(run.organization_id, "automation.step.resumed", run, step)

        graph = ReactFlowGraph(**run.graph_snapshot)
        _dispatch_ready_downstream(session, run, graph, step.node_id)
        _check_run_completion(session, run)


@celery_app.task(queue=AUTOMATION_QUEUE)
def trigger_scheduled_pipeline(pipeline_id: str) -> None:
    """Called by Celery Beat for scheduled pipelines."""
    with WorkerSession() as session:
        from app.models.automation import AutomationPipeline, AutomationRun, AutomationRunStep
        from app.automation.engine import create_run_sync, get_root_node_ids
        from app.schemas.automation import ReactFlowGraph
        from sqlalchemy import select, and_

        pipeline = session.get(AutomationPipeline, uuid.UUID(pipeline_id))
        if not pipeline or pipeline.status != "active" or pipeline.deleted_at:
            return

        # Concurrency guard: skip if there's already a running/pending run for this pipeline
        active_run_count = session.query(AutomationRun).filter(
            AutomationRun.pipeline_id == pipeline.id,
            AutomationRun.status.in_(["pending", "running"]),
        ).count()
        if active_run_count > 0:
            logger.info(
                "Skipping scheduled run for pipeline %s — %d active run(s)",
                pipeline_id, active_run_count,
            )
            return

        run = create_run_sync(
            session, pipeline, "schedule",
            trigger_data={"scheduled_at": datetime.now(UTC).isoformat()},
        )
        session.commit()

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