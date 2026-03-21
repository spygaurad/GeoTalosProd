"""Add tile_source_id to map_layers.

Revision ID: 7e8f9a0b1c2d
Revises: 6d7e8f9a0b1c
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "7e8f9a0b1c2d"
down_revision = "6d7e8f9a0b1c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "map_layers",
        sa.Column("tile_source_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_map_layers_tile_source",
        "map_layers",
        "tile_sources",
        ["tile_source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_map_layers_tile_source", "map_layers", ["tile_source_id"])

    # Drop old check constraint and create updated one
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL AND tile_source_id IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL AND tile_source_id IS NULL) OR
        (source_type = 'tile_service' AND (tile_source_id IS NOT NULL OR tile_service_url IS NOT NULL) AND dataset_id IS NULL AND stac_item_id IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'annotation_set' AND annotation_set_id IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL AND tile_service_url IS NULL AND tile_source_id IS NULL)
        """
    )


def downgrade():
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'annotation_set' AND annotation_set_id IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL AND tile_service_url IS NULL)
        """
    )
    op.drop_index("idx_map_layers_tile_source")
    op.drop_constraint("fk_map_layers_tile_source", "map_layers", type_="foreignkey")
    op.drop_column("map_layers", "tile_source_id")
