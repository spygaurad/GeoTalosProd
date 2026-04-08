"""Create project_annotation_sets.

Revision ID: 4c5d6e7f
Revises: 3b4c5d6e
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "4c5d6e7f"
down_revision = "3b4c5d6e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_annotation_sets",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("linked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["annotation_set_id"], ["annotation_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "annotation_set_id"),
    )
    op.create_index(
        "idx_project_annotation_sets_set",
        "project_annotation_sets",
        ["annotation_set_id"],
    )

    op.execute(
        """
        INSERT INTO project_annotation_sets (project_id, annotation_set_id)
        SELECT DISTINCT m.project_id, aset.id
        FROM annotation_sets aset
        JOIN maps m ON m.id = aset.map_id
        WHERE aset.map_id IS NOT NULL
        ON CONFLICT (project_id, annotation_set_id) DO NOTHING
        """
    )

    op.execute("ALTER TABLE project_annotation_sets ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY project_annotation_sets_select ON project_annotation_sets
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
        CREATE POLICY project_annotation_sets_insert ON project_annotation_sets
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
        CREATE POLICY project_annotation_sets_update ON project_annotation_sets
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
        CREATE POLICY project_annotation_sets_delete ON project_annotation_sets
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
    op.execute("DROP POLICY IF EXISTS project_annotation_sets_delete ON project_annotation_sets")
    op.execute("DROP POLICY IF EXISTS project_annotation_sets_update ON project_annotation_sets")
    op.execute("DROP POLICY IF EXISTS project_annotation_sets_insert ON project_annotation_sets")
    op.execute("DROP POLICY IF EXISTS project_annotation_sets_select ON project_annotation_sets")
    op.execute("ALTER TABLE project_annotation_sets DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_project_annotation_sets_set", table_name="project_annotation_sets")
    op.drop_table("project_annotation_sets")
