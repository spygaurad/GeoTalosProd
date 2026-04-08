"""Create project_datasets.

Revision ID: 6e7f8091
Revises: 5d6e7f80
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "6e7f8091"
down_revision = "5d6e7f80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_datasets",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("linked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "dataset_id"),
    )
    op.create_index("idx_project_datasets_dataset", "project_datasets", ["dataset_id"])

    op.execute(
        """
        INSERT INTO project_datasets (project_id, dataset_id)
        SELECT DISTINCT m.project_id, ml.dataset_id
        FROM map_layers ml
        JOIN maps m ON m.id = ml.map_id
        WHERE ml.dataset_id IS NOT NULL
        ON CONFLICT (project_id, dataset_id) DO NOTHING
        """
    )

    op.execute("ALTER TABLE project_datasets ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY project_datasets_select ON project_datasets
            FOR SELECT TO app_user
            USING (
                project_id IN (
                    SELECT id FROM projects
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY project_datasets_insert ON project_datasets
            FOR INSERT TO app_user
            WITH CHECK (
                project_id IN (
                    SELECT id FROM projects
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY project_datasets_update ON project_datasets
            FOR UPDATE TO app_user
            USING (
                project_id IN (
                    SELECT id FROM projects
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
            WITH CHECK (
                project_id IN (
                    SELECT id FROM projects
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY project_datasets_delete ON project_datasets
            FOR DELETE TO app_user
            USING (
                project_id IN (
                    SELECT id FROM projects
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS project_datasets_delete ON project_datasets")
    op.execute("DROP POLICY IF EXISTS project_datasets_update ON project_datasets")
    op.execute("DROP POLICY IF EXISTS project_datasets_insert ON project_datasets")
    op.execute("DROP POLICY IF EXISTS project_datasets_select ON project_datasets")
    op.execute("ALTER TABLE project_datasets DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_project_datasets_dataset", table_name="project_datasets")
    op.drop_table("project_datasets")
