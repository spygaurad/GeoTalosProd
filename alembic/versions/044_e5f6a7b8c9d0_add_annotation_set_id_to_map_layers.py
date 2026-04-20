"""Add annotation_set_id to map_layers for raster/vector annotation set layers.

Enables annotation sets (raster segmentation masks and vector GeoJSON sets)
to be added as proper MapLayer records on a map.  Introduces a new
source_type value 'annotation_set' that is enforced by an updated check
constraint.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "map_layers",
        sa.Column("annotation_set_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_map_layers_annotation_set",
        "map_layers",
        "annotation_sets",
        ["annotation_set_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_map_layers_annotation_set", "map_layers", ["annotation_set_id"])

    # Drop the existing check constraint and replace with one that includes the
    # new 'annotation_set' source_type branch.
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset'
            AND dataset_id IS NOT NULL
            AND stac_item_id IS NULL
            AND tile_service_url IS NULL
            AND tile_source_id IS NULL
            AND annotation_set_id IS NULL)
        OR
        (source_type = 'stac_item'
            AND stac_item_id IS NOT NULL
            AND dataset_id IS NULL
            AND tile_service_url IS NULL
            AND tile_source_id IS NULL
            AND annotation_set_id IS NULL)
        OR
        (source_type = 'tile_service'
            AND (tile_source_id IS NOT NULL OR tile_service_url IS NOT NULL)
            AND dataset_id IS NULL
            AND stac_item_id IS NULL
            AND annotation_set_id IS NULL)
        OR
        (source_type = 'annotation_set'
            AND annotation_set_id IS NOT NULL
            AND dataset_id IS NULL
            AND stac_item_id IS NULL
            AND tile_service_url IS NULL
            AND tile_source_id IS NULL)
        """,
    )


def downgrade() -> None:
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
    op.drop_index("idx_map_layers_annotation_set", table_name="map_layers")
    op.drop_constraint("fk_map_layers_annotation_set", "map_layers", type_="foreignkey")
    op.drop_column("map_layers", "annotation_set_id")
