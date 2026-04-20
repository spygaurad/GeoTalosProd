"""Add ai_model schema binding and model class mappings.

Revision ID: 7f8091a2
Revises: 6e7f8091
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "7f8091a2"
down_revision = "6e7f8091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_models", sa.Column("annotation_schema_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "ai_models",
        sa.Column("output_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_models_annotation_schema",
        "ai_models",
        "annotation_schemas",
        ["annotation_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "model_class_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("uuid_generate_v7()")),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_label", sa.String(length=255), nullable=False),
        sa.Column("annotation_class_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["model_id"], ["ai_models.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["annotation_class_id"], ["annotation_classes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", "model_label", name="uq_model_class_mappings_model_label"),
    )
    op.create_index("idx_model_class_mappings_model", "model_class_mappings", ["model_id"])

    op.execute("ALTER TABLE model_class_mappings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY model_class_mappings_select ON model_class_mappings
            FOR SELECT TO app_user
            USING (
                model_id IN (
                    SELECT id FROM ai_models
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY model_class_mappings_insert ON model_class_mappings
            FOR INSERT TO app_user
            WITH CHECK (
                model_id IN (
                    SELECT id FROM ai_models
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY model_class_mappings_update ON model_class_mappings
            FOR UPDATE TO app_user
            USING (
                model_id IN (
                    SELECT id FROM ai_models
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
            WITH CHECK (
                model_id IN (
                    SELECT id FROM ai_models
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )
    op.execute(
        """
        CREATE POLICY model_class_mappings_delete ON model_class_mappings
            FOR DELETE TO app_user
            USING (
                model_id IN (
                    SELECT id FROM ai_models
                    WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
                )
            )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS model_class_mappings_delete ON model_class_mappings")
    op.execute("DROP POLICY IF EXISTS model_class_mappings_update ON model_class_mappings")
    op.execute("DROP POLICY IF EXISTS model_class_mappings_insert ON model_class_mappings")
    op.execute("DROP POLICY IF EXISTS model_class_mappings_select ON model_class_mappings")
    op.execute("ALTER TABLE model_class_mappings DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_model_class_mappings_model", table_name="model_class_mappings")
    op.drop_table("model_class_mappings")

    op.drop_constraint("fk_ai_models_annotation_schema", "ai_models", type_="foreignkey")
    op.drop_column("ai_models", "output_config")
    op.drop_column("ai_models", "annotation_schema_id")
