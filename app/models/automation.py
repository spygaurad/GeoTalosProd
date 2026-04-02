import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ─── Enums (stored as varchar, validated in Pydantic) ─────────────────────

# AutomationPipeline.trigger_type: manual | schedule | event
# AutomationPipeline.status:       draft | active | paused | archived
# AutomationRun.status:            pending | running | completed | failed | cancelled
# AutomationRunStep.status:        pending | running | completed | failed | skipped


class AutomationPipeline(Base):
    """
    A user-defined automation pipeline stored as a ReactFlow graph.
    Scoped to a project within an organization.
    """
    __tablename__ = "automation_pipelines"
    __table_args__ = (
        Index("idx_automation_pipelines_org", "organization_id"),
        Index("idx_automation_pipelines_project", "project_id"),
        Index("idx_automation_pipelines_status", "status"),
        Index("idx_automation_pipelines_trigger_type", "trigger_type"),
        CheckConstraint(
            "trigger_type IN ('manual', 'schedule', 'event')",
            name="ck_automation_pipelines_trigger_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_automation_pipelines_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default="manual")
    trigger_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Raw ReactFlow JSON: { nodes: [...], edges: [...] }
    # Each node has: { id, type, position, data: { config: {...} } }
    graph: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default='{"nodes":[],"edges":[]}')
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="draft")
    # Cached metadata for quick listing without parsing graph
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship("Organization")
    project: Mapped["Project"] = relationship("Project")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    runs: Mapped[list["AutomationRun"]] = relationship(
        "AutomationRun", back_populates="pipeline", cascade="all, delete-orphan"
    )


class AutomationRun(Base):
    """
    A single execution of a pipeline. Created when a pipeline is triggered.
    """
    __tablename__ = "automation_runs"
    __table_args__ = (
        Index("idx_automation_runs_pipeline", "pipeline_id"),
        Index("idx_automation_runs_org", "organization_id"),
        Index("idx_automation_runs_project", "project_id"),
        Index("idx_automation_runs_status", "status"),
        Index("idx_automation_runs_started_at", "started_at"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_automation_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("automation_pipelines.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    # Snapshot of the graph at run time (so edits to pipeline don't affect in-flight runs)
    graph_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    trigger_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Aggregate progress
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    completed_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    progress: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.0")
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    pipeline: Mapped["AutomationPipeline"] = relationship("AutomationPipeline", back_populates="runs")
    organization: Mapped["Organization"] = relationship("Organization")
    project: Mapped["Project"] = relationship("Project")
    triggered_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[triggered_by])
    steps: Mapped[list["AutomationRunStep"]] = relationship(
        "AutomationRunStep", back_populates="run", cascade="all, delete-orphan"
    )


class AutomationRunStep(Base):
    """
    Tracks execution of a single node within a pipeline run.
    Maps 1:1 to a ReactFlow node in the graph snapshot.
    """
    __tablename__ = "automation_run_steps"
    __table_args__ = (
        Index("idx_automation_run_steps_run", "run_id"),
        Index("idx_automation_run_steps_org", "organization_id"),
        Index("idx_automation_run_steps_status", "status"),
        Index("idx_automation_run_steps_node_type", "node_type"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'waiting_for_job')",
            name="ck_automation_run_steps_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Matches the ReactFlow node.id string (e.g., "node_1", "abc-123")
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    node_type: Mapped[str] = mapped_column(String(100), nullable=False)
    node_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="pending")
    # User-configured params for this node (from ReactFlow node.data.config)
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Resolved inputs from upstream steps (populated at execution time)
    input_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Output data produced by this step (passed to downstream steps)
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # For branching nodes: which output handle was activated
    active_output_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # If status='waiting_for_job', this is the Job.id we're waiting on
    waiting_for_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retry tracking
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    run: Mapped["AutomationRun"] = relationship("AutomationRun", back_populates="steps")
