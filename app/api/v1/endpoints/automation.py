"""Automation pipeline endpoints — CRUD, execution, monitoring."""
from datetime import datetime, UTC
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.core.exceptions import not_found
from app.models.automation import AutomationPipeline, AutomationRun, AutomationRunStep
from app.models.user import User
from app.schemas.automation import (
    GraphValidationResult,
    NodeCatalogResponse,
    NodeTypeDef,
    HandleDef as HandleDefSchema,
    PipelineCreate,
    PipelineList,
    PipelineRead,
    PipelineSummary,
    PipelineUpdate,
    RunDetailRead,
    RunList,
    RunRead,
    RunTriggerRequest,
    StepRead,
)

router = APIRouter(prefix="/automation", tags=["automation"])

# ── Handle type registry for the catalog ──────────────────────────────────
HANDLE_TYPES = [
    {"type": "trigger_data", "label": "Trigger Data", "description": "Pipeline trigger context", "color": "#9333EA"},
    {"type": "dataset", "label": "Dataset", "description": "A dataset reference", "color": "#3B82F6"},
    {"type": "dataset_items", "label": "Dataset Items", "description": "List of COG files", "color": "#3B82F6"},
    {"type": "annotation_set", "label": "Annotation Set", "description": "Set of labeled geometries", "color": "#10B981"},
    {"type": "model", "label": "Model", "description": "ML model reference", "color": "#8B5CF6"},
    {"type": "raw_predictions", "label": "Raw Predictions", "description": "Unprocessed model output", "color": "#F59E0B"},
    {"type": "processed_predictions", "label": "Processed Predictions", "description": "Post-processed predictions", "color": "#F59E0B"},
    {"type": "matched_pairs", "label": "Matched Pairs", "description": "IoU-matched annotation pairs", "color": "#F59E0B"},
    {"type": "quality_metrics", "label": "Quality Metrics", "description": "Statistical metrics", "color": "#EF4444"},
    {"type": "tracked_objects", "label": "Tracked Objects", "description": "Tracked object references", "color": "#06B6D4"},
    {"type": "string", "label": "String", "description": "Text value (URL, path, etc.)", "color": "#6B7280"},
    {"type": "any", "label": "Any", "description": "Accepts any data type", "color": "#6B7280"},
]

# Category display metadata
CATEGORY_META = {
    "triggers": {"label": "Triggers", "icon": "zap"},
    "data_source": {"label": "Data Source", "icon": "database"},
    "ml_annotation": {"label": "ML Annotation", "icon": "brain"},
    "iou_quality": {"label": "IoU / Quality", "icon": "check-circle"},
    "analysis": {"label": "Analysis", "icon": "activity"},
    "map_overlay": {"label": "Map / Overlay", "icon": "layers"},
    "output": {"label": "Output", "icon": "send"},
    "data_operations": {"label": "Data Operations", "icon": "git-merge"},
    "advanced": {"label": "Advanced", "icon": "cpu"},
}


# ── Helpers ────────────────────────────────────────────────────────────────

async def _get_pipeline(session: AsyncSession, pipeline_id: UUID, org_id: UUID) -> AutomationPipeline:
    result = await session.execute(
        select(AutomationPipeline).where(
            AutomationPipeline.id == pipeline_id,
            AutomationPipeline.organization_id == org_id,
            AutomationPipeline.deleted_at.is_(None),
        )
    )
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise not_found("Pipeline")
    return pipeline


async def _get_run(session: AsyncSession, run_id: UUID, org_id: UUID) -> AutomationRun:
    result = await session.execute(
        select(AutomationRun).where(
            AutomationRun.id == run_id,
            AutomationRun.organization_id == org_id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise not_found("Run")
    return run


# ── 3a. Node Catalog ──────────────────────────────────────────────────────

@router.get("/node-catalog", response_model=NodeCatalogResponse)
async def get_node_catalog(
    category: str | None = Query(None),
    _org_id: UUID = Depends(require_org_role("org:viewer")),
):
    from app.automation.registry import get_catalog

    by_category = get_catalog()
    categories = []
    for cat_name, nodes in sorted(by_category.items()):
        if category and cat_name != category:
            continue
        meta = CATEGORY_META.get(cat_name, {"label": cat_name.replace("_", " ").title(), "icon": "box"})
        categories.append({
            "name": cat_name,
            "label": meta["label"],
            "icon": meta["icon"],
            "nodes": [
                NodeTypeDef(
                    type=n.type,
                    category=n.category,
                    label=n.label,
                    description=n.description,
                    icon=n.icon,
                    inputs=[HandleDefSchema(handle=h.handle, type=h.type, required=h.required, multiple=h.multiple, label=h.label) for h in n.inputs],
                    outputs=[HandleDefSchema(handle=h.handle, type=h.type, required=h.required, multiple=h.multiple, label=h.label) for h in n.outputs],
                    config_schema=n.config_schema,
                    status=n.status,
                    frontend_preview=n.frontend_preview,
                    color=n.color,
                ).model_dump()
                for n in nodes
            ],
        })

    return NodeCatalogResponse(categories=categories, handle_types=HANDLE_TYPES)


# ── 3b. Pipeline CRUD ─────────────────────────────────────────────────────

@router.get("/pipelines", response_model=PipelineList)
async def list_pipelines(
    project_id: UUID = Query(...),
    pipeline_status: str | None = Query(None, alias="status"),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    conditions = [
        AutomationPipeline.organization_id == org_id,
        AutomationPipeline.project_id == project_id,
        AutomationPipeline.deleted_at.is_(None),
    ]
    if pipeline_status:
        conditions.append(AutomationPipeline.status == pipeline_status)

    total_result = await db.execute(
        select(func.count()).select_from(AutomationPipeline).where(*conditions)
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(AutomationPipeline)
        .where(*conditions)
        .order_by(AutomationPipeline.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    pipelines = result.scalars().all()
    return PipelineList(
        items=[PipelineSummary.model_validate(p) for p in pipelines],
        total=total, limit=limit, offset=offset,
    )


@router.post("/pipelines", response_model=PipelineRead, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    body: PipelineCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    graph_dict = body.graph.model_dump()
    pipeline = AutomationPipeline(
        organization_id=org_id,
        project_id=body.project_id,
        name=body.name,
        description=body.description,
        trigger_type=body.trigger_type.value,
        trigger_config=body.trigger_config.model_dump() if body.trigger_config else None,
        graph=graph_dict,
        status=body.status.value,
        node_count=len(body.graph.nodes),
        created_by=user.id,
    )
    db.add(pipeline)
    await db.flush()
    await db.refresh(pipeline)

    await log_audit_event(
        action="automation_pipeline.created",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_pipeline",
        entity_id=str(pipeline.id),
        session=db,
    )
    await db.commit()
    await db.refresh(pipeline)
    return PipelineRead.model_validate(pipeline)


@router.get("/pipelines/{pipeline_id}", response_model=PipelineRead)
async def get_pipeline(
    pipeline_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    pipeline = await _get_pipeline(db, pipeline_id, org_id)
    return PipelineRead.model_validate(pipeline)


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineRead)
async def update_pipeline(
    pipeline_id: UUID,
    body: PipelineUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    pipeline = await _get_pipeline(db, pipeline_id, org_id)

    update_data = body.model_dump(exclude_unset=True)
    if "graph" in update_data and body.graph is not None:
        update_data["graph"] = body.graph.model_dump()
        update_data["node_count"] = len(body.graph.nodes)
    if "trigger_type" in update_data and body.trigger_type is not None:
        update_data["trigger_type"] = body.trigger_type.value
    if "trigger_config" in update_data and body.trigger_config is not None:
        update_data["trigger_config"] = body.trigger_config.model_dump()
    if "status" in update_data and body.status is not None:
        update_data["status"] = body.status.value

    for key, value in update_data.items():
        setattr(pipeline, key, value)

    # Manage scheduled trigger
    if pipeline.trigger_type == "schedule" and pipeline.status == "active":
        from app.workers.automation.scheduler import register_pipeline_schedule
        tc = pipeline.trigger_config or {}
        if tc.get("cron_expression"):
            register_pipeline_schedule(
                str(pipeline.id), tc["cron_expression"], tc.get("timezone", "UTC")
            )
    elif pipeline.status in ("paused", "archived", "draft"):
        from app.workers.automation.scheduler import unregister_pipeline_schedule
        unregister_pipeline_schedule(str(pipeline.id))

    await log_audit_event(
        action="automation_pipeline.updated",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_pipeline",
        entity_id=str(pipeline.id),
        session=db,
    )
    await db.commit()
    await db.refresh(pipeline)
    return PipelineRead.model_validate(pipeline)


@router.delete("/pipelines/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(
    pipeline_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    pipeline = await _get_pipeline(db, pipeline_id, org_id)
    pipeline.deleted_at = datetime.now(UTC).replace(tzinfo=None)
    pipeline.status = "archived"

    from app.workers.automation.scheduler import unregister_pipeline_schedule
    unregister_pipeline_schedule(str(pipeline.id))

    await log_audit_event(
        action="automation_pipeline.deleted",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_pipeline",
        entity_id=str(pipeline.id),
        session=db,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/pipelines/{pipeline_id}/validate", response_model=GraphValidationResult)
async def validate_pipeline(
    pipeline_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    from app.automation.engine import validate_graph
    from app.schemas.automation import ReactFlowGraph

    pipeline = await _get_pipeline(db, pipeline_id, org_id)
    graph = ReactFlowGraph(**pipeline.graph)
    return validate_graph(graph)


@router.post("/pipelines/{pipeline_id}/duplicate", response_model=PipelineRead, status_code=status.HTTP_201_CREATED)
async def duplicate_pipeline(
    pipeline_id: UUID,
    name: str | None = Query(None),
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    source = await _get_pipeline(db, pipeline_id, org_id)
    new_pipeline = AutomationPipeline(
        organization_id=org_id,
        project_id=source.project_id,
        name=name or f"Copy of {source.name}",
        description=source.description,
        trigger_type=source.trigger_type,
        trigger_config=source.trigger_config,
        graph=source.graph,
        status="draft",
        node_count=source.node_count,
        created_by=user.id,
    )
    db.add(new_pipeline)
    await db.flush()
    await db.refresh(new_pipeline)

    await log_audit_event(
        action="automation_pipeline.duplicated",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_pipeline",
        entity_id=str(new_pipeline.id),
        extra={"source_pipeline_id": str(pipeline_id)},
        session=db,
    )
    await db.commit()
    await db.refresh(new_pipeline)
    return PipelineRead.model_validate(new_pipeline)


# ── 3c. Pipeline Execution ────────────────────────────────────────────────

@router.post("/pipelines/{pipeline_id}/run", status_code=status.HTTP_202_ACCEPTED, response_model=RunRead)
async def run_pipeline(
    pipeline_id: UUID,
    body: RunTriggerRequest | None = None,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    from app.automation.engine import create_run, validate_graph, get_root_node_ids
    from app.schemas.automation import ReactFlowGraph

    pipeline = await _get_pipeline(db, pipeline_id, org_id)
    graph = ReactFlowGraph(**pipeline.graph)

    # Validate first
    validation = validate_graph(graph)
    if not validation.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Pipeline graph is invalid",
                "errors": [e.model_dump() for e in validation.errors],
            },
        )

    trigger_data = body.trigger_data if body else None
    run = await create_run(db, pipeline, pipeline.trigger_type, trigger_data, triggered_by=user.id)
    await db.flush()

    # Collect root steps to dispatch
    root_ids = get_root_node_ids(graph)
    result = await db.execute(
        select(AutomationRunStep).where(
            AutomationRunStep.run_id == run.id,
            AutomationRunStep.node_id.in_(root_ids),
        )
    )
    root_steps = result.scalars().all()

    await log_audit_event(
        action="automation_pipeline.run_triggered",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_run",
        entity_id=str(run.id),
        extra={"pipeline_id": str(pipeline_id)},
        session=db,
    )
    await db.commit()
    await db.refresh(run)

    # Dispatch after commit
    from app.workers.automation.tasks import execute_step
    for step in root_steps:
        execute_step.delay(str(run.id), str(step.id))

    return RunRead.model_validate(run)


@router.get("/pipelines/{pipeline_id}/runs", response_model=RunList)
async def list_pipeline_runs(
    pipeline_id: UUID,
    run_status: str | None = Query(None, alias="status"),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    # Verify pipeline access
    await _get_pipeline(db, pipeline_id, org_id)

    conditions = [
        AutomationRun.pipeline_id == pipeline_id,
        AutomationRun.organization_id == org_id,
    ]
    if run_status:
        conditions.append(AutomationRun.status == run_status)

    total_result = await db.execute(
        select(func.count()).select_from(AutomationRun).where(*conditions)
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(AutomationRun)
        .where(*conditions)
        .order_by(AutomationRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = result.scalars().all()
    return RunList(
        items=[RunRead.model_validate(r) for r in runs],
        total=total, limit=limit, offset=offset,
    )


# ── 3d. Run Monitoring ────────────────────────────────────────────────────

@router.get("/runs/{run_id}", response_model=RunDetailRead)
async def get_run(
    run_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    run = await _get_run(db, run_id, org_id)
    steps_result = await db.execute(
        select(AutomationRunStep)
        .where(AutomationRunStep.run_id == run.id)
        .order_by(AutomationRunStep.created_at)
    )
    steps = steps_result.scalars().all()

    run_data = RunRead.model_validate(run).model_dump()
    run_data["steps"] = [StepRead.model_validate(s) for s in steps]
    run_data["graph_snapshot"] = run.graph_snapshot
    return RunDetailRead(**run_data)


@router.get("/runs/{run_id}/steps", response_model=list[StepRead])
async def list_run_steps(
    run_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    await _get_run(db, run_id, org_id)
    result = await db.execute(
        select(AutomationRunStep)
        .where(AutomationRunStep.run_id == run_id)
        .order_by(AutomationRunStep.created_at)
    )
    return [StepRead.model_validate(s) for s in result.scalars().all()]


@router.get("/runs/{run_id}/steps/{step_id}", response_model=StepRead)
async def get_run_step(
    run_id: UUID,
    step_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    await _get_run(db, run_id, org_id)
    result = await db.execute(
        select(AutomationRunStep).where(
            AutomationRunStep.id == step_id,
            AutomationRunStep.run_id == run_id,
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise not_found("Step")
    return StepRead.model_validate(step)


@router.get("/runs/{run_id}/steps/{step_id}/download")
async def download_step_output(
    run_id: UUID,
    step_id: UUID,
    redirect: bool = Query(False),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Get a presigned download URL for an export step's output file."""
    import asyncio
    from fastapi.responses import RedirectResponse
    from app.services import storage_service

    await _get_run(db, run_id, org_id)
    result = await db.execute(
        select(AutomationRunStep).where(
            AutomationRunStep.id == step_id,
            AutomationRunStep.run_id == run_id,
        )
    )
    step = result.scalar_one_or_none()
    if not step:
        raise not_found("Step")
    if not step.output_data:
        raise HTTPException(status_code=404, detail="Step has no output data")

    # Find the first s3:// URL in output_data values
    s3_url = None
    for value in step.output_data.values():
        if isinstance(value, str) and value.startswith("s3://"):
            s3_url = value
            break

    if not s3_url:
        raise HTTPException(status_code=404, detail="No downloadable file in step output")

    # Parse s3://bucket/key
    path = s3_url[5:]  # strip "s3://"
    _bucket, _, s3_key = path.partition("/")
    if not s3_key:
        raise HTTPException(status_code=404, detail="Invalid S3 URL in step output")

    download_url = await asyncio.to_thread(
        storage_service.generate_download_url, org_id, s3_key
    )

    if redirect:
        return RedirectResponse(url=download_url, status_code=302)
    return {"download_url": download_url}


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
async def cancel_run(
    run_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    run = await _get_run(db, run_id, org_id)
    if run.status not in ("pending", "running"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel run in '{run.status}' state")

    run.status = "cancelled"
    run.completed_at = datetime.now(UTC).replace(tzinfo=None)

    # Skip all pending/waiting steps
    pending_result = await db.execute(
        select(AutomationRunStep).where(
            AutomationRunStep.run_id == run.id,
            AutomationRunStep.status.in_(["pending", "waiting_for_job"]),
        )
    )
    for step in pending_result.scalars().all():
        step.status = "skipped"

    # Update pipeline status
    pipeline_result = await db.execute(
        select(AutomationPipeline).where(AutomationPipeline.id == run.pipeline_id)
    )
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline:
        pipeline.last_run_status = "cancelled"

    # Revoke pending Celery tasks
    celery_task_result = await db.execute(
        select(AutomationRunStep.celery_task_id).where(
            AutomationRunStep.run_id == run.id,
            AutomationRunStep.status == "running",
            AutomationRunStep.celery_task_id.isnot(None),
        )
    )
    task_ids = [r[0] for r in celery_task_result.all()]

    await log_audit_event(
        action="automation_run.cancelled",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_run",
        entity_id=str(run.id),
        session=db,
    )
    await db.commit()

    # Revoke after commit
    if task_ids:
        from app.workers.celery_app import celery_app
        for tid in task_ids:
            celery_app.control.revoke(tid, terminate=False)

    await db.refresh(run)
    return RunRead.model_validate(run)


@router.post("/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=RunRead)
async def retry_run(
    run_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    from app.automation.engine import get_root_node_ids
    from app.schemas.automation import ReactFlowGraph

    original_run = await _get_run(db, run_id, org_id)
    if original_run.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot retry run in '{original_run.status}' state")

    # Load original steps
    steps_result = await db.execute(
        select(AutomationRunStep).where(AutomationRunStep.run_id == original_run.id)
    )
    original_steps = {s.node_id: s for s in steps_result.scalars().all()}

    # Create a new run with the same graph snapshot
    graph = ReactFlowGraph(**original_run.graph_snapshot)
    new_run = AutomationRun(
        pipeline_id=original_run.pipeline_id,
        organization_id=org_id,
        project_id=original_run.project_id,
        status="pending",
        graph_snapshot=original_run.graph_snapshot,
        trigger_type=original_run.trigger_type,
        trigger_data=original_run.trigger_data,
        total_steps=original_run.total_steps,
        triggered_by=user.id,
    )
    db.add(new_run)
    await db.flush()

    # Create steps — copy completed outputs, re-run failed/skipped
    completed_count = 0
    needs_dispatch: list[str] = []
    for node in graph.nodes:
        orig_step = original_steps.get(node.id)
        if orig_step and orig_step.status == "completed":
            # Copy completed step as-is
            step = AutomationRunStep(
                run_id=new_run.id,
                organization_id=org_id,
                node_id=node.id,
                node_type=orig_step.node_type,
                node_label=orig_step.node_label,
                status="completed",
                config=orig_step.config,
                input_data=orig_step.input_data,
                output_data=orig_step.output_data,
                started_at=orig_step.started_at,
                completed_at=orig_step.completed_at,
                duration_ms=orig_step.duration_ms,
            )
            completed_count += 1
        else:
            from app.automation.registry import get_node_type
            from app.automation.engine import _resolve_node_type
            resolved = _resolve_node_type(node)
            node_type = get_node_type(resolved)
            step = AutomationRunStep(
                run_id=new_run.id,
                organization_id=org_id,
                node_id=node.id,
                node_type=resolved,
                node_label=node.data.get("label", node_type.label if node_type else resolved),
                config=node.data.get("config"),
            )
            needs_dispatch.append(node.id)
        db.add(step)

    new_run.completed_steps = completed_count
    new_run.progress = completed_count / new_run.total_steps if new_run.total_steps > 0 else 0

    await log_audit_event(
        action="automation_run.retried",
        actor_id=str(user.id),
        organization_id=str(org_id),
        entity="automation_run",
        entity_id=str(new_run.id),
        extra={"original_run_id": str(run_id)},
        session=db,
    )
    await db.commit()

    # Dispatch steps that need re-running and have all upstream completed
    from app.automation.engine import get_upstream_edges
    from app.workers.automation.tasks import execute_step

    # Reload steps for the new run
    new_steps_result = await db.execute(
        select(AutomationRunStep).where(AutomationRunStep.run_id == new_run.id)
    )
    new_steps_map = {s.node_id: s for s in new_steps_result.scalars().all()}

    root_ids = get_root_node_ids(graph)
    for node_id in needs_dispatch:
        # Check if all upstream are completed (either from copy or root)
        upstream = get_upstream_edges(graph, node_id)
        if not upstream:
            # Root node — dispatch immediately
            step = new_steps_map[node_id]
            execute_step.delay(str(new_run.id), str(step.id))
        else:
            upstream_node_ids = {e.source for e in upstream}
            all_ready = all(
                new_steps_map.get(uid) and new_steps_map[uid].status == "completed"
                for uid in upstream_node_ids
            )
            if all_ready:
                step = new_steps_map[node_id]
                execute_step.delay(str(new_run.id), str(step.id))

    await db.refresh(new_run)
    return RunRead.model_validate(new_run)


# ── 3e. Project-Level Run List ─────────────────────────────────────────────

@router.get("/projects/{project_id}/runs", response_model=RunList)
async def list_project_runs(
    project_id: UUID,
    pipeline_id: UUID | None = Query(None),
    run_status: str | None = Query(None, alias="status"),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    conditions = [
        AutomationRun.organization_id == org_id,
        AutomationRun.project_id == project_id,
    ]
    if pipeline_id:
        conditions.append(AutomationRun.pipeline_id == pipeline_id)
    if run_status:
        conditions.append(AutomationRun.status == run_status)

    total_result = await db.execute(
        select(func.count()).select_from(AutomationRun).where(*conditions)
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(AutomationRun)
        .where(*conditions)
        .order_by(AutomationRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    runs = result.scalars().all()
    return RunList(
        items=[RunRead.model_validate(r) for r in runs],
        total=total, limit=limit, offset=offset,
    )
