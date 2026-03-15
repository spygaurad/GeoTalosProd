"""create annotation classes

Revision ID: 8192a3b4
Revises: 708192a3
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "8192a3b4"
down_revision = "708192a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    op.create_table(
        "annotation_classes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "schema_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation_schemas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation_classes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column(
            "style_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("styles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.execute(
        "ALTER TABLE annotation_classes ALTER COLUMN path TYPE ltree USING path::ltree"
    )
    op.create_index("idx_annotation_classes_schema", "annotation_classes", ["schema_id"])
    op.create_index("idx_annotation_classes_parent", "annotation_classes", ["parent_id"])
    op.create_index("idx_annotation_classes_style", "annotation_classes", ["style_id"])
    op.create_index(
        "idx_annotation_classes_path",
        "annotation_classes",
        ["path"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("idx_annotation_classes_path", table_name="annotation_classes")
    op.drop_index("idx_annotation_classes_style", table_name="annotation_classes")
    op.drop_index("idx_annotation_classes_parent", table_name="annotation_classes")
    op.drop_index("idx_annotation_classes_schema", table_name="annotation_classes")
    op.drop_table("annotation_classes")
