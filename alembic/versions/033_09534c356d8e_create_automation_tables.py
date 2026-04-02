"""create automation tables

Revision ID: 033_a1b2c3d4
Revises: 032_add_tile_source_to_map_layers
Create Date: 2026-03-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "09534c356d8e"
down_revision = "7e8f9a0b1c2d"  # 032_add_tile_source_to_map_layers
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── automation_pipelines ──────────────────────────────────────────────
    op.create_table(
        "automation_pipelines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v7()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("trigger_type", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("trigger_config", JSONB, nullable=True),
        sa.Column("graph", JSONB, nullable=False, server_default='{"nodes":[],"edges":[]}'),
        sa.Column("status", sa.String(50), nullable=False, server_default="draft"),
        sa.Column("node_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(50), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("trigger_type IN ('manual', 'schedule', 'event')", name="ck_automation_pipelines_trigger_type"),
        sa.CheckConstraint("status IN ('draft', 'active', 'paused', 'archived')", name="ck_automation_pipelines_status"),
    )
    op.create_index("idx_automation_pipelines_org", "automation_pipelines", ["organization_id"])
    op.create_index("idx_automation_pipelines_project", "automation_pipelines", ["project_id"])
    op.create_index("idx_automation_pipelines_status", "automation_pipelines", ["status"])
    op.create_index("idx_automation_pipelines_trigger_type", "automation_pipelines", ["trigger_type"])

    # ── automation_runs ───────────────────────────────────────────────────
    op.create_table(
        "automation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v7()")),
        sa.Column("pipeline_id", UUID(as_uuid=True), sa.ForeignKey("automation_pipelines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("graph_snapshot", JSONB, nullable=False),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("trigger_data", JSONB, nullable=True),
        sa.Column("total_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completed_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("progress", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("triggered_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'cancelled')", name="ck_automation_runs_status"),
    )
    op.create_index("idx_automation_runs_pipeline", "automation_runs", ["pipeline_id"])
    op.create_index("idx_automation_runs_org", "automation_runs", ["organization_id"])
    op.create_index("idx_automation_runs_project", "automation_runs", ["project_id"])
    op.create_index("idx_automation_runs_status", "automation_runs", ["status"])
    op.create_index("idx_automation_runs_started_at", "automation_runs", ["started_at"])

    # ── automation_run_steps ──────────────────────────────────────────────
    op.create_table(
        "automation_run_steps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v7()")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(255), nullable=False),
        sa.Column("node_type", sa.String(100), nullable=False),
        sa.Column("node_label", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("config", JSONB, nullable=True),
        sa.Column("input_data", JSONB, nullable=True),
        sa.Column("output_data", JSONB, nullable=True),
        sa.Column("active_output_handle", sa.String(100), nullable=True),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("waiting_for_job_id", UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'waiting_for_job')", name="ck_automation_run_steps_status"),
    )
    op.create_index("idx_automation_run_steps_run", "automation_run_steps", ["run_id"])
    op.create_index("idx_automation_run_steps_org", "automation_run_steps", ["organization_id"])
    op.create_index("idx_automation_run_steps_status", "automation_run_steps", ["status"])
    op.create_index("idx_automation_run_steps_node_type", "automation_run_steps", ["node_type"])

    # ── RLS ────────────────────────────────────────────────────────────────
    for table in ("automation_pipelines", "automation_runs", "automation_run_steps"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # All three tables: direct org-scoped RLS
    for table in ("automation_pipelines", "automation_runs", "automation_run_steps"):
        op.execute(f"""
            CREATE POLICY {table}_org_isolation ON {table}
            USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """)

    # ── updated_at trigger ─────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER set_updated_at
        BEFORE UPDATE ON automation_pipelines
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS set_updated_at ON automation_pipelines")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    for table in ("automation_run_steps", "automation_runs", "automation_pipelines"):
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.drop_table(table)
