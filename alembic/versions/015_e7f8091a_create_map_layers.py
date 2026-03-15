"""create map layers

Revision ID: e7f8091a
Revises: d6e7f809
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e7f8091a"
down_revision = "d6e7f809"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "map_layers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "map_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("maps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("layer_type", sa.String(length=50), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datasets.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("stac_item_id", sa.String(length=255), nullable=True),
        sa.Column("tile_service_url", sa.String(length=500), nullable=True),
        sa.Column("source_config", postgresql.JSONB(), nullable=True),
        sa.Column(
            "style_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("styles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("style_override", postgresql.JSONB(), nullable=True),
        sa.Column("time_config", postgresql.JSONB(), nullable=True),
        sa.Column("z_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("opacity", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("min_zoom", sa.Integer(), nullable=True),
        sa.Column("max_zoom", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL) OR "
            "(source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL) OR "
            "(source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL)",
            name="ck_map_layers_source",
        ),
    )
    op.create_index("idx_map_layers_map", "map_layers", ["map_id"])
    op.create_index("idx_map_layers_dataset", "map_layers", ["dataset_id"])
    op.create_index("idx_map_layers_style", "map_layers", ["style_id"])


def downgrade() -> None:
    op.drop_index("idx_map_layers_style", table_name="map_layers")
    op.drop_index("idx_map_layers_dataset", table_name="map_layers")
    op.drop_index("idx_map_layers_map", table_name="map_layers")
    op.drop_table("map_layers")
