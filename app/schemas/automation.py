from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel, PaginatedResponse


# ─── Enums ────────────────────────────────────────────────────────────────

class TriggerType(str, Enum):
    manual = "manual"
    schedule = "schedule"
    event = "event"

class PipelineStatus(str, Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    archived = "archived"

class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    waiting_for_job = "waiting_for_job"


# ─── Node Catalog Schemas (for GET /automation/node-catalog) ──────────────

class HandleDef(BaseModel):
    """Defines a single input or output handle on a node type."""
    handle: str                          # Handle ID (e.g., "items", "annotations")
    type: str                            # Handle type for edge validation (e.g., "dataset_items")
    required: bool = True
    multiple: bool = False               # True if this handle accepts multiple connections
    label: str | None = None             # Display label in UI

class NodeTypeDef(BaseModel):
    """Complete definition of a node type — returned by the catalog endpoint."""
    type: str                            # Unique node type ID (e.g., "run_inference")
    category: str                        # UI sidebar group (e.g., "ml_annotation")
    label: str                           # Display name
    description: str                     # Tooltip / help text
    icon: str | None = None              # Icon identifier for UI
    inputs: list[HandleDef]
    outputs: list[HandleDef]
    config_schema: dict[str, Any]        # JSON Schema for the node's config form
    status: str = "implemented"          # "implemented" or "placeholder"
    frontend_preview: bool = False       # True if UI can compute a live preview client-side
    # UI hints
    color: str | None = None             # Node background color
    min_width: int | None = None

class NodeCatalogResponse(BaseModel):
    """Full node catalog grouped by category."""
    categories: list[dict[str, Any]]     # [{ name, label, icon, nodes: [NodeTypeDef] }]
    handle_types: list[dict[str, str]]   # [{ type, label, description, color }]


# ─── Pipeline Schemas ─────────────────────────────────────────────────────

class ReactFlowNode(BaseModel):
    """A single node in the ReactFlow graph."""
    id: str
    type: str
    position: dict[str, float]           # { x: float, y: float }
    data: dict[str, Any]                 # { config: {...}, label: str, ... }
    width: float | None = None
    height: float | None = None

class ReactFlowEdge(BaseModel):
    """A single edge connecting two nodes."""
    id: str
    source: str                          # Source node ID
    sourceHandle: str                    # Source output handle ID
    target: str                          # Target node ID
    targetHandle: str                    # Target input handle ID
    animated: bool | None = None
    label: str | None = None

class ReactFlowGraph(BaseModel):
    """Complete ReactFlow graph — stored as JSONB in automation_pipelines.graph."""
    nodes: list[ReactFlowNode]
    edges: list[ReactFlowEdge]
    viewport: dict[str, float] | None = None  # { x, y, zoom } — for restoring canvas position

class TriggerConfig(BaseModel):
    """Configuration for pipeline triggers."""
    # schedule trigger
    cron_expression: str | None = None   # e.g., "0 9 * * 1" (Mondays at 9am)
    timezone: str | None = "UTC"
    # event trigger
    event_type: str | None = None        # e.g., "dataset.ingested", "annotation.created"
    event_filters: dict[str, Any] | None = None  # e.g., { "dataset_id": "..." }

class PipelineCreate(BaseModel):
    project_id: UUID | None = None
    name: str = Field(max_length=255)
    description: str | None = None
    trigger_type: TriggerType = TriggerType.manual
    trigger_config: TriggerConfig | None = None
    graph: ReactFlowGraph = ReactFlowGraph(nodes=[], edges=[])
    status: PipelineStatus = PipelineStatus.draft

class PipelineUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    trigger_type: TriggerType | None = None
    trigger_config: TriggerConfig | None = None
    graph: ReactFlowGraph | None = None
    status: PipelineStatus | None = None


class PipelineDuplicateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    target_project_id: UUID | None = None
    target_map_id: UUID | None = None
    target_aoi_id: UUID | None = None

class PipelineRead(ORMModel):
    id: UUID
    organization_id: UUID
    project_id: UUID | None
    name: str
    description: str | None
    trigger_type: str
    trigger_config: dict | None
    graph: dict                          # Raw ReactFlow JSON
    status: str
    node_count: int
    last_run_at: datetime | None
    last_run_status: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

class PipelineSummary(ORMModel):
    """Lightweight version for list endpoints — excludes full graph."""
    id: UUID
    organization_id: UUID
    project_id: UUID | None
    name: str
    description: str | None
    trigger_type: str
    status: str
    node_count: int
    last_run_at: datetime | None
    last_run_status: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


# ─── Run Schemas ──────────────────────────────────────────────────────────

class RunTriggerRequest(BaseModel):
    """Request body for manually triggering a pipeline run."""
    trigger_data: dict[str, Any] | None = None  # Optional overrides / context

class RunRead(ORMModel):
    id: UUID
    pipeline_id: UUID
    organization_id: UUID
    project_id: UUID | None
    status: str
    trigger_type: str
    trigger_data: dict | None
    total_steps: int
    completed_steps: int
    failed_steps: int
    progress: float
    triggered_by: UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    created_at: datetime

class RunDetailRead(RunRead):
    """Run with all step details — for the run detail/monitoring page."""
    steps: list["StepRead"]
    graph_snapshot: dict                 # ReactFlow graph at time of run


# ─── Step Schemas ─────────────────────────────────────────────────────────

class StepRead(ORMModel):
    id: UUID
    run_id: UUID
    node_id: str
    node_type: str
    node_label: str | None
    status: str
    config: dict | None
    input_data: dict | None
    output_data: dict | None
    active_output_handle: str | None
    celery_task_id: str | None
    waiting_for_job_id: UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    error: str | None
    attempt: int
    max_retries: int
    created_at: datetime


# ─── Validation Schemas ───────────────────────────────────────────────────

class GraphValidationError(BaseModel):
    node_id: str | None = None
    edge_id: str | None = None
    error_type: str                      # "missing_input", "type_mismatch", "cycle", "unknown_node_type"
    message: str

class GraphValidationResult(BaseModel):
    valid: bool
    errors: list[GraphValidationError]
    warnings: list[GraphValidationError]
    # Computed metadata
    execution_order: list[str] | None = None  # Topologically sorted node IDs
    node_count: int = 0
    edge_count: int = 0


# ─── Paginated Responses ─────────────────────────────────────────────────

PipelineList = PaginatedResponse[PipelineSummary]
RunList = PaginatedResponse[RunRead]
