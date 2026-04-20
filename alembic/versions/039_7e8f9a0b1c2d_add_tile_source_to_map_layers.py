"""Add tile_source_id to map_layers.

Revision ID: 7e8f9a0b1c2d
Revises: 6d7e8f9a0b1c
Create Date: 2026-03-21

NOTE: annotation_set_id was already dropped from map_layers in migration 033
(drop_legacy_annotation_set_map_links). Annotation sets are now mounted on maps
via the map_annotation_sets join table. This migration only adds tile_source_id
and updates the check constraint accordingly.
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

    # Drop the check constraint left by migration 033 (no annotation_set_id, no tile_source_id)
    # and replace with one that includes tile_source_id.
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL AND tile_source_id IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL AND tile_source_id IS NULL) OR
        (source_type = 'tile_service' AND (tile_source_id IS NOT NULL OR tile_service_url IS NOT NULL) AND dataset_id IS NULL AND stac_item_id IS NULL)
        """,
    )


def downgrade():
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    # Restore constraint as it was after migration 033 (no annotation_set_id, no tile_source_id)
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL) OR
        (source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL)
        """,
    )
    op.drop_index("idx_map_layers_tile_source")
    op.drop_constraint("fk_map_layers_tile_source", "map_layers", type_="foreignkey")
    op.drop_column("map_layers", "tile_source_id")
