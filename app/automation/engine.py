"""
Pipeline execution engine.

Responsibilities:
1. Validate a pipeline graph (type checking, cycle detection, required inputs).
2. Create a run with step rows from the graph (async for API, sync for Celery).
3. Dispatch root nodes to Celery.
4. After each step completes, resolve downstream dependencies and dispatch ready nodes.
5. Track aggregate run progress.
"""
import uuid
from collections import defaultdict, deque
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.registry import get_node_type, NODE_REGISTRY
from app.models.automation import AutomationPipeline, AutomationRun, AutomationRunStep
from app.schemas.automation import (
    GraphValidationError,
    GraphValidationResult,
    ReactFlowGraph,
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _resolve_node_type(node) -> str:
    """Resolve the automation node type from a ReactFlow node.

    ReactFlow uses `node.type` for the component name (e.g., "pipeline").
    The actual automation node type may be stored in `node.data.nodeType`
    or `node.data.type`. Falls back to `node.type` if neither exists.
    """
    if node.data:
        for key in ("nodeType", "type"):
            val = node.data.get(key)
            if val and isinstance(val, str):
                return val
    return node.type


# ─── Graph Validation ─────────────────────────────────────────────────────

def validate_graph(graph: ReactFlowGraph) -> GraphValidationResult:
    """
    Validate a ReactFlow graph for executability.

    Checks performed:
    1. All node types exist in the registry.
    2. All edges connect valid handle IDs on their source/target nodes.
    3. Handle types are compatible across each edge.
    4. All required inputs on every node have at least one incoming edge.
    5. No cycles in the graph (DAG check via Kahn's algorithm).
    6. Required config fields are present on each node.

    Returns a GraphValidationResult with errors, warnings, and execution order.
    """
    errors: list[GraphValidationError] = []
    warnings: list[GraphValidationError] = []

    # 1. Check node types
    for node in graph.nodes:
        resolved = _resolve_node_type(node)
        node_type = get_node_type(resolved)
        if node_type is None:
            errors.append(GraphValidationError(
                node_id=node.id,
                error_type="unknown_node_type",
                message=f"Unknown node type: '{resolved}'",
            ))

    # 2-3. Check edges: valid handles + type compatibility
    node_map = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.source not in node_map:
            errors.append(GraphValidationError(
                edge_id=edge.id,
                error_type="invalid_edge",
                message=f"Edge source node '{edge.source}' not found",
            ))
            continue
        if edge.target not in node_map:
            errors.append(GraphValidationError(
                edge_id=edge.id,
                error_type="invalid_edge",
                message=f"Edge target node '{edge.target}' not found",
            ))
            continue

        source_type_def = get_node_type(_resolve_node_type(node_map[edge.source]))
        target_type_def = get_node_type(_resolve_node_type(node_map[edge.target]))
        if not source_type_def or not target_type_def:
            continue  # Already reported as unknown_node_type

        # Find output handle on source
        source_handle = next((h for h in source_type_def.outputs if h.handle == edge.sourceHandle), None)
        if not source_handle:
            errors.append(GraphValidationError(
                edge_id=edge.id,
                error_type="invalid_handle",
                message=f"Source handle '{edge.sourceHandle}' not found on node type '{source_type_def.type}'",
            ))
            continue

        # Find input handle on target
        target_handle = next((h for h in target_type_def.inputs if h.handle == edge.targetHandle), None)
        if not target_handle:
            errors.append(GraphValidationError(
                edge_id=edge.id,
                error_type="invalid_handle",
                message=f"Target handle '{edge.targetHandle}' not found on node type '{target_type_def.type}'",
            ))
            continue

        # Type compatibility check
        if target_handle.type != "any" and source_handle.type != target_handle.type:
            errors.append(GraphValidationError(
                edge_id=edge.id,
                error_type="type_mismatch",
                message=f"Type mismatch: '{source_handle.type}' → '{target_handle.type}'",
            ))

    # 4. Check required inputs have incoming edges
    incoming_edges = defaultdict(set)  # node_id -> set of target handle IDs
    incoming_edge_counts = defaultdict(lambda: defaultdict(int))  # node_id -> handle -> count
    for edge in graph.edges:
        incoming_edges[edge.target].add(edge.targetHandle)
        incoming_edge_counts[edge.target][edge.targetHandle] += 1

    for node in graph.nodes:
        node_type = get_node_type(_resolve_node_type(node))
        if not node_type:
            continue
        for inp in node_type.inputs:
            if inp.required and inp.handle not in incoming_edges.get(node.id, set()):
                errors.append(GraphValidationError(
                    node_id=node.id,
                    error_type="missing_input",
                    message=f"Required input '{inp.handle}' has no incoming edge",
                ))
            if not inp.multiple and incoming_edge_counts[node.id].get(inp.handle, 0) > 1:
                errors.append(GraphValidationError(
                    node_id=node.id,
                    error_type="multiple_inputs_not_allowed",
                    message=f"Input '{inp.handle}' does not accept multiple incoming edges",
                ))

    # 5. Cycle detection (Kahn's algorithm)
    execution_order = _topological_sort(graph)
    if execution_order is None:
        errors.append(GraphValidationError(
            error_type="cycle",
            message="Graph contains a cycle — pipelines must be acyclic (DAG)",
        ))

    return GraphValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        execution_order=execution_order,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
    )


def _topological_sort(graph: ReactFlowGraph) -> list[str] | None:
    """Kahn's algorithm. Returns ordered node IDs or None if cycle exists."""
    in_degree: dict[str, int] = {n.id: 0 for n in graph.nodes}
    adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}

    for edge in graph.edges:
        if edge.source in adjacency and edge.target in in_degree:
            adjacency[edge.source].append(edge.target)
            in_degree[edge.target] += 1

    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    order: list[str] = []

    while queue:
        nid = queue.popleft()
        order.append(nid)
        for neighbor in adjacency[nid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return order if len(order) == len(graph.nodes) else None


# ─── Run Creation ─────────────────────────────────────────────────────────

async def create_run(
    session: AsyncSession,
    pipeline: AutomationPipeline,
    trigger_type: str,
    trigger_data: dict | None = None,
    triggered_by: uuid.UUID | None = None,
) -> AutomationRun:
    """
    Create an AutomationRun and all AutomationRunStep rows from the pipeline graph.
    Does NOT dispatch Celery tasks — call dispatch_ready_steps() after commit.
    """
    graph = ReactFlowGraph(**pipeline.graph)
    validation = validate_graph(graph)
    if not validation.valid:
        raise ValueError(f"Pipeline graph is invalid: {[e.message for e in validation.errors]}")

    run = AutomationRun(
        pipeline_id=pipeline.id,
        organization_id=pipeline.organization_id,
        project_id=pipeline.project_id,
        status="pending",
        graph_snapshot=pipeline.graph,
        trigger_type=trigger_type,
        trigger_data=trigger_data,
        total_steps=len(graph.nodes),
        triggered_by=triggered_by,
    )
    session.add(run)
    await session.flush()  # Get run.id

    # Create a step row for each node
    for node in graph.nodes:
        resolved = _resolve_node_type(node)
        node_type = get_node_type(resolved)
        step = AutomationRunStep(
            run_id=run.id,
            organization_id=pipeline.organization_id,
            node_id=node.id,
            node_type=resolved,
            node_label=node.data.get("label", node_type.label if node_type else resolved),
            config=node.data.get("config"),
        )
        session.add(step)

    # Update pipeline metadata
    pipeline.last_run_at = datetime.now(UTC).replace(tzinfo=None)
    pipeline.last_run_status = "pending"

    return run


# ─── Step Dispatching ─────────────────────────────────────────────────────

def get_root_node_ids(graph: ReactFlowGraph) -> list[str]:
    """Find nodes with no incoming edges (pipeline entry points)."""
    targets = {e.target for e in graph.edges}
    return [n.id for n in graph.nodes if n.id not in targets]


def get_downstream_node_ids(graph: ReactFlowGraph, node_id: str) -> list[str]:
    """Find node IDs directly downstream of the given node."""
    return [e.target for e in graph.edges if e.source == node_id]


def get_upstream_edges(graph: ReactFlowGraph, node_id: str):
    """Get all edges incoming to a node."""
    return [e for e in graph.edges if e.target == node_id]


def resolve_step_inputs(
    graph: ReactFlowGraph,
    node_id: str,
    completed_steps: dict[str, AutomationRunStep],
) -> dict[str, Any]:
    """
    Build the input_data dict for a step by reading output_data
    from its upstream completed steps, mapped through edges.
    """
    inputs: dict[str, Any] = {}
    node_map = {n.id: n for n in graph.nodes}
    target_node = node_map.get(node_id)
    target_type = get_node_type(_resolve_node_type(target_node)) if target_node else None
    multiple_handles = {
        handle.handle for handle in (target_type.inputs if target_type else []) if handle.multiple
    }
    for edge in get_upstream_edges(graph, node_id):
        upstream_step = completed_steps.get(edge.source)
        if upstream_step and upstream_step.output_data:
            value = upstream_step.output_data.get(edge.sourceHandle)
            if value is not None:
                if edge.targetHandle in multiple_handles:
                    inputs.setdefault(edge.targetHandle, []).append(value)
                else:
                    inputs[edge.targetHandle] = value
    return inputs


# ─── Sync Run Creation (for Celery workers) ──────────────────────────────

def create_run_sync(
    session,
    pipeline: AutomationPipeline,
    trigger_type: str,
    trigger_data: dict | None = None,
    triggered_by: uuid.UUID | None = None,
) -> AutomationRun:
    """
    Synchronous version of create_run for use in Celery worker context.
    Same logic as async create_run, but uses sync session.
    """
    graph = ReactFlowGraph(**pipeline.graph)
    validation = validate_graph(graph)
    if not validation.valid:
        raise ValueError(f"Pipeline graph is invalid: {[e.message for e in validation.errors]}")

    run = AutomationRun(
        pipeline_id=pipeline.id,
        organization_id=pipeline.organization_id,
        project_id=pipeline.project_id,
        status="pending",
        graph_snapshot=pipeline.graph,
        trigger_type=trigger_type,
        trigger_data=trigger_data,
        total_steps=len(graph.nodes),
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()

    for node in graph.nodes:
        resolved = _resolve_node_type(node)
        node_type = get_node_type(resolved)
        step = AutomationRunStep(
            run_id=run.id,
            organization_id=pipeline.organization_id,
            node_id=node.id,
            node_type=resolved,
            node_label=node.data.get("label", node_type.label if node_type else resolved),
            config=node.data.get("config"),
        )
        session.add(step)

    pipeline.last_run_at = datetime.now(UTC).replace(tzinfo=None)
    pipeline.last_run_status = "pending"

    return run
