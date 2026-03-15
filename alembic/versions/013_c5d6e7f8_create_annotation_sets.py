"""create annotation sets

Revision ID: c5d6e7f8
Revises: b4c5d6e7
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c5d6e7f8"
down_revision = "b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotation_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "schema_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation_schemas.id"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datasets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stac_item_id", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(created_by_user_id IS NOT NULL AND created_by_job_id IS NULL) OR "
            "(created_by_user_id IS NULL AND created_by_job_id IS NOT NULL)",
            name="ck_annotation_sets_creator",
        ),
    )
    op.create_index("idx_annotation_sets_map", "annotation_sets", ["map_id"])
    op.create_index("idx_annotation_sets_schema", "annotation_sets", ["schema_id"])
    op.create_index("idx_annotation_sets_dataset", "annotation_sets", ["dataset_id"])
    op.create_index("idx_annotation_sets_stac_item", "annotation_sets", ["stac_item_id"])
    op.create_index("idx_annotation_sets_created_by_user", "annotation_sets", ["created_by_user_id"])
    op.create_index("idx_annotation_sets_created_by_job", "annotation_sets", ["created_by_job_id"])


def downgrade() -> None:
    op.drop_index("idx_annotation_sets_created_by_job", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_created_by_user", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_stac_item", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_dataset", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_schema", table_name="annotation_sets")
    op.drop_index("idx_annotation_sets_map", table_name="annotation_sets")
    op.drop_table("annotation_sets")
