"""Create dataset_items table.

Revision ID: 1c2d3e4f5a6b
Revises: 0b1c2d3e4f50
Create Date: 2026-03-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "1c2d3e4f5a6b"
down_revision = "0b1c2d3e4f50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stac_item_id", sa.String(255), nullable=False),
        sa.Column("stac_collection_id", sa.String(255), nullable=False),
        sa.Column("s3_uri", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column(
            "geometry",
            postgresql.JSONB(),
            nullable=True,
            comment="GeoJSON geometry of the item footprint",
        ),
        sa.Column("item_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("properties_cache", postgresql.JSONB(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("stac_item_id", name="uq_dataset_items_stac_item_id"),
    )
    op.create_index("idx_dataset_items_dataset", "dataset_items", ["dataset_id"])
    op.create_index("idx_dataset_items_org", "dataset_items", ["organization_id"])
    op.create_index(
        "idx_dataset_items_active",
        "dataset_items",
        ["dataset_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("idx_dataset_items_active", table_name="dataset_items")
    op.drop_index("idx_dataset_items_org", table_name="dataset_items")
    op.drop_index("idx_dataset_items_dataset", table_name="dataset_items")
    op.drop_table("dataset_items")
