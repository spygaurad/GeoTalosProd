"""Restructure annotation_sets to org scope with provenance.

Revision ID: 3b4c5d6e
Revises: 9165194191ce
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "3b4c5d6e"
down_revision = "9165194191ce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("annotation_sets", sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("annotation_sets", sa.Column("dataset_item_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "annotation_sets",
        sa.Column("source_type", sa.String(length=50), server_default="manual", nullable=False),
    )
    op.add_column("annotation_sets", sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("annotation_sets", sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True))

    op.execute(
        """
        UPDATE annotation_sets AS aset
        SET organization_id = p.organization_id
        FROM maps m
        JOIN projects p ON p.id = m.project_id
        WHERE aset.map_id = m.id
        """
    )
    op.execute("UPDATE annotation_sets SET job_id = created_by_job_id WHERE created_by_job_id IS NOT NULL")

    op.create_foreign_key(
        "fk_annotation_sets_org",
        "annotation_sets",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_annotation_sets_dataset_item",
        "annotation_sets",
        "dataset_items",
        ["dataset_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_annotation_sets_model",
        "annotation_sets",
        "ai_models",
        ["model_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_annotation_sets_job",
        "annotation_sets",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index("idx_annotation_sets_org", "annotation_sets", ["organization_id"])
    op.create_index("idx_annotation_sets_dataset_item", "annotation_sets", ["dataset_item_id"])
    op.create_index("idx_annotation_sets_source_type", "annotation_sets", ["source_type"])
    op.create_index("idx_annotation_sets_model", "annotation_sets", ["model_id"])
    op.create_index("idx_annotation_sets_job", "annotation_sets", ["job_id"])

    op.drop_index("idx_annotation_sets_stac_item", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_created_by_job", table_name="annotation_sets")
    op.drop_constraint("ck_annotation_sets_creator", "annotation_sets", type_="check")
    op.drop_constraint("annotation_sets_created_by_job_id_fkey", "annotation_sets", type_="foreignkey")
    op.drop_column("annotation_sets", "created_by_job_id")
    op.drop_column("annotation_sets", "stac_item_id")

    op.create_check_constraint(
        "ck_annotation_sets_creator",
        "annotation_sets",
        "(created_by_user_id IS NOT NULL) OR (job_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_annotation_sets_source_type",
        "annotation_sets",
        "source_type IN ('manual', 'model', 'import', 'analysis')",
    )

    op.alter_column("annotation_sets", "map_id", nullable=True)
    op.alter_column("annotation_sets", "organization_id", nullable=False)

    op.execute("DROP POLICY IF EXISTS annotation_sets_select ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_insert ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_update ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_delete ON annotation_sets")
    op.execute(
        """
        CREATE POLICY annotation_sets_select ON annotation_sets
            FOR SELECT TO app_user
            USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_insert ON annotation_sets
            FOR INSERT TO app_user
            WITH CHECK (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_update ON annotation_sets
            FOR UPDATE TO app_user
            USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_delete ON annotation_sets
            FOR DELETE TO app_user
            USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS annotation_sets_delete ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_update ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_insert ON annotation_sets")
    op.execute("DROP POLICY IF EXISTS annotation_sets_select ON annotation_sets")

    op.execute(
        """
        CREATE POLICY annotation_sets_select ON annotation_sets
            FOR SELECT TO app_user
            USING (
                schema_id IN (
                    SELECT id FROM annotation_schemas
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_insert ON annotation_sets
            FOR INSERT TO app_user
            WITH CHECK (
                schema_id IN (
                    SELECT id FROM annotation_schemas
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_update ON annotation_sets
            FOR UPDATE TO app_user
            USING (
                schema_id IN (
                    SELECT id FROM annotation_schemas
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
            WITH CHECK (
                schema_id IN (
                    SELECT id FROM annotation_schemas
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY annotation_sets_delete ON annotation_sets
            FOR DELETE TO app_user
            USING (
                schema_id IN (
                    SELECT id FROM annotation_schemas
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )

    op.alter_column("annotation_sets", "map_id", nullable=False)
    op.drop_constraint("ck_annotation_sets_source_type", "annotation_sets", type_="check")
    op.drop_constraint("ck_annotation_sets_creator", "annotation_sets", type_="check")
    op.create_check_constraint(
        "ck_annotation_sets_creator",
        "annotation_sets",
        "(created_by_user_id IS NOT NULL AND created_by_job_id IS NULL) OR "
        "(created_by_user_id IS NULL AND created_by_job_id IS NOT NULL)",
    )

    op.add_column("annotation_sets", sa.Column("stac_item_id", sa.String(length=255), nullable=True))
    op.add_column("annotation_sets", sa.Column("created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "annotation_sets_created_by_job_id_fkey",
        "annotation_sets",
        "jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_annotation_sets_stac_item", "annotation_sets", ["stac_item_id"])
    op.create_index("idx_annotation_sets_created_by_job", "annotation_sets", ["created_by_job_id"])
    op.execute("UPDATE annotation_sets SET created_by_job_id = job_id WHERE job_id IS NOT NULL")

    op.drop_index("idx_annotation_sets_job", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_model", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_source_type", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_dataset_item", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_org", table_name="annotation_sets")
    op.drop_constraint("fk_annotation_sets_job", "annotation_sets", type_="foreignkey")
    op.drop_constraint("fk_annotation_sets_model", "annotation_sets", type_="foreignkey")
    op.drop_constraint("fk_annotation_sets_dataset_item", "annotation_sets", type_="foreignkey")
    op.drop_constraint("fk_annotation_sets_org", "annotation_sets", type_="foreignkey")
    op.drop_column("annotation_sets", "job_id")
    op.drop_column("annotation_sets", "model_id")
    op.drop_column("annotation_sets", "source_type")
    op.drop_column("annotation_sets", "dataset_item_id")
    op.drop_column("annotation_sets", "organization_id")
