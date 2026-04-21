"""Stage 1 — map AOI + MapLayer render/filter/aoi + FeatureLayer role.

Columns added:

- ``maps.aoi_geometry``                (Geometry GEOMETRY 4326) — first-class
  map-level AOI feeding mosaic ``intersects=`` and vector ``ST_Intersects``.
- ``map_layers.dataset_item_id``        FK → dataset_items.id (CASCADE).
- ``map_layers.feature_layer_id``       FK → feature_layers.id (CASCADE).
- ``map_layers.basemap_id``             FK → basemaps.id (CASCADE).
- ``map_layers.render_config``          JSONB — rescale/colormap/expression/nodata/assets/band_index.
- ``map_layers.filter_config``          JSONB — per-layer attribute filter
  (e.g. ``{confidence: {gte: 0.7}}`` or ``{class_id: {in: [...]}}``).  Gap
  identified during plan validation — surfaced here so Stage 3 resolver can
  consume it without another migration.
- ``map_layers.aoi_filter``             (Geometry GEOMETRY 4326) — per-layer AOI override.
- ``map_layers.deleted_at``             DateTime — align soft-delete with the rest of the schema.

CHECK constraint rewrite: covers all 8 canonical ``source_type`` values from
the plan PLUS the legacy names (``dataset``, ``stac_item``, ``tile_service``)
so existing rows keep validating.  Stage 3 will UPDATE rows to canonical
names and drop the legacy branches.

``feature_layers.role`` (``reference``|``aoi``|``sketch``, default
``reference``) with CHECK.

Revision ID: bb2c3d4e5f60
Revises: aa1b2c3d4e5f
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "bb2c3d4e5f60"
down_revision = "aa1b2c3d4e5f"
branch_labels = None
depends_on = None


_SOURCE_CHECK = """
(source_type = 'dataset_item'    AND (dataset_item_id IS NOT NULL OR stac_item_id IS NOT NULL)) OR
(source_type = 'stac_item'       AND stac_item_id IS NOT NULL) OR
(source_type = 'dataset_mosaic'  AND dataset_id IS NOT NULL) OR
(source_type = 'dataset'         AND dataset_id IS NOT NULL) OR
(source_type = 'stac_search'     AND source_config ? 'searchid') OR
(source_type = 'annotation_set'  AND annotation_set_id IS NOT NULL) OR
(source_type = 'feature_layer'   AND feature_layer_id IS NOT NULL) OR
(source_type = 'tile_source'     AND tile_source_id IS NOT NULL) OR
(source_type = 'tile_service'    AND (tile_source_id IS NOT NULL OR tile_service_url IS NOT NULL)) OR
(source_type = 'basemap'         AND basemap_id IS NOT NULL) OR
(source_type = 'xarray_variable' AND source_config ? 'variable_ref')
"""

_LEGACY_SOURCE_CHECK = """
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
"""


def upgrade() -> None:
    # ─── maps.aoi_geometry ─────────────────────────────────────────────────
    op.add_column(
        "maps",
        sa.Column(
            "aoi_geometry",
            Geometry("GEOMETRY", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )
    op.execute(
        "CREATE INDEX idx_maps_aoi_geometry ON maps USING GIST (aoi_geometry)"
    )

    # ─── map_layers new columns ────────────────────────────────────────────
    op.add_column(
        "map_layers",
        sa.Column("dataset_item_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_map_layers_dataset_item",
        "map_layers",
        "dataset_items",
        ["dataset_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_map_layers_dataset_item", "map_layers", ["dataset_item_id"]
    )

    op.add_column(
        "map_layers",
        sa.Column("feature_layer_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_map_layers_feature_layer",
        "map_layers",
        "feature_layers",
        ["feature_layer_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_map_layers_feature_layer", "map_layers", ["feature_layer_id"]
    )

    op.add_column(
        "map_layers",
        sa.Column("basemap_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_map_layers_basemap",
        "map_layers",
        "basemaps",
        ["basemap_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_map_layers_basemap", "map_layers", ["basemap_id"])

    op.add_column(
        "map_layers", sa.Column("render_config", JSONB, nullable=True)
    )
    op.add_column(
        "map_layers", sa.Column("filter_config", JSONB, nullable=True)
    )
    op.add_column(
        "map_layers",
        sa.Column(
            "aoi_filter",
            Geometry("GEOMETRY", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )
    op.execute(
        "CREATE INDEX idx_map_layers_aoi_filter ON map_layers USING GIST (aoi_filter)"
    )
    op.add_column(
        "map_layers",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ─── Rewrite ck_map_layers_source with full 8-branch coverage ──────────
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source", "map_layers", _SOURCE_CHECK
    )

    # ─── feature_layers.role ───────────────────────────────────────────────
    op.add_column(
        "feature_layers",
        sa.Column(
            "role",
            sa.String(length=30),
            nullable=False,
            server_default="reference",
        ),
    )
    op.create_check_constraint(
        "ck_feature_layers_role",
        "feature_layers",
        "role IN ('reference', 'aoi', 'sketch')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_feature_layers_role", "feature_layers", type_="check")
    op.drop_column("feature_layers", "role")

    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source", "map_layers", _LEGACY_SOURCE_CHECK
    )

    op.drop_column("map_layers", "deleted_at")
    op.execute("DROP INDEX IF EXISTS idx_map_layers_aoi_filter")
    op.drop_column("map_layers", "aoi_filter")
    op.drop_column("map_layers", "filter_config")
    op.drop_column("map_layers", "render_config")

    op.drop_index("idx_map_layers_basemap", table_name="map_layers")
    op.drop_constraint("fk_map_layers_basemap", "map_layers", type_="foreignkey")
    op.drop_column("map_layers", "basemap_id")

    op.drop_index("idx_map_layers_feature_layer", table_name="map_layers")
    op.drop_constraint(
        "fk_map_layers_feature_layer", "map_layers", type_="foreignkey"
    )
    op.drop_column("map_layers", "feature_layer_id")

    op.drop_index("idx_map_layers_dataset_item", table_name="map_layers")
    op.drop_constraint(
        "fk_map_layers_dataset_item", "map_layers", type_="foreignkey"
    )
    op.drop_column("map_layers", "dataset_item_id")

    op.execute("DROP INDEX IF EXISTS idx_maps_aoi_geometry")
    op.drop_column("maps", "aoi_geometry")
