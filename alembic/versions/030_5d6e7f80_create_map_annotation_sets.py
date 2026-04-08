"""Create map_annotation_sets.

Revision ID: 5d6e7f80
Revises: 4c5d6e7f
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "5d6e7f80"
down_revision = "4c5d6e7f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "map_annotation_sets",
        sa.Column("map_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("opacity", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("style_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("style_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mounted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["map_id"], ["maps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["annotation_set_id"], ["annotation_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["style_id"], ["styles.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("map_id", "annotation_set_id"),
    )
    op.create_index(
        "idx_map_annotation_sets_set",
        "map_annotation_sets",
        ["annotation_set_id"],
    )

    op.execute(
        """
        INSERT INTO map_annotation_sets (map_id, annotation_set_id)
        SELECT aset.map_id, aset.id
        FROM annotation_sets aset
        WHERE aset.map_id IS NOT NULL
        ON CONFLICT (map_id, annotation_set_id) DO NOTHING
        """
    )

    op.execute("ALTER TABLE map_annotation_sets ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY map_annotation_sets_select ON map_annotation_sets
            FOR SELECT TO app_user
            USING (
                map_id IN (
                    SELECT m.id FROM maps m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY map_annotation_sets_insert ON map_annotation_sets
            FOR INSERT TO app_user
            WITH CHECK (
                map_id IN (
                    SELECT m.id FROM maps m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY map_annotation_sets_update ON map_annotation_sets
            FOR UPDATE TO app_user
            USING (
                map_id IN (
                    SELECT m.id FROM maps m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
            WITH CHECK (
                map_id IN (
                    SELECT m.id FROM maps m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY map_annotation_sets_delete ON map_annotation_sets
            FOR DELETE TO app_user
            USING (
                map_id IN (
                    SELECT m.id FROM maps m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS map_annotation_sets_delete ON map_annotation_sets")
    op.execute("DROP POLICY IF EXISTS map_annotation_sets_update ON map_annotation_sets")
    op.execute("DROP POLICY IF EXISTS map_annotation_sets_insert ON map_annotation_sets")
    op.execute("DROP POLICY IF EXISTS map_annotation_sets_select ON map_annotation_sets")
    op.execute("ALTER TABLE map_annotation_sets DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_map_annotation_sets_set", table_name="map_annotation_sets")
    op.drop_table("map_annotation_sets")
