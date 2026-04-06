"""Drop legacy annotation_set map ownership and map_layer annotation_set source.

Revision ID: 8091a2b3
Revises: 7f8091a2
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "8091a2b3"
down_revision = "7f8091a2"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def _check_exists(inspector: sa.Inspector, table_name: str, check_name: str) -> bool:
    return any(chk["name"] == check_name for chk in inspector.get_check_constraints(table_name))


def _drop_fk_by_name_or_column(
    inspector: sa.Inspector,
    table_name: str,
    fk_name: str,
    column_name: str,
) -> None:
    fk_to_drop = None

    # First try exact name match.
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("name") == fk_name:
            fk_to_drop = fk_name
            break

    # Fallback: resolve FK by constrained column.
    if fk_to_drop is None:
        for fk in inspector.get_foreign_keys(table_name):
            if column_name in (fk.get("constrained_columns") or []):
                fk_to_drop = fk.get("name")
                break

    if fk_to_drop:
        op.drop_constraint(fk_to_drop, table_name, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _index_exists(inspector, "annotation_sets", "idx_annotation_sets_map"):
        op.drop_index("idx_annotation_sets_map", table_name="annotation_sets")

    if _column_exists(inspector, "annotation_sets", "map_id"):
        _drop_fk_by_name_or_column(
            inspector,
            table_name="annotation_sets",
            fk_name="annotation_sets_map_id_fkey",
            column_name="map_id",
        )
        op.drop_column("annotation_sets", "map_id")

    if _check_exists(inspector, "map_layers", "ck_map_layers_source"):
        op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL) OR
        (source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL)
        """,
    )

    if _column_exists(inspector, "map_layers", "annotation_set_id"):
        _drop_fk_by_name_or_column(
            inspector,
            table_name="map_layers",
            fk_name="idx_map_layers_annotation_set",
            column_name="annotation_set_id",
        )
        op.drop_column("map_layers", "annotation_set_id")


def downgrade() -> None:
    op.add_column("map_layers", sa.Column("annotation_set_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "idx_map_layers_annotation_set",
        "map_layers",
        "annotation_sets",
        ["annotation_set_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("ck_map_layers_source", "map_layers", type_="check")
    op.create_check_constraint(
        "ck_map_layers_source",
        "map_layers",
        """
        (source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL AND annotation_set_id IS NULL) OR
        (source_type = 'annotation_set' AND annotation_set_id IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL AND tile_service_url IS NULL)
        """,
    )

    op.add_column("annotation_sets", sa.Column("map_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "annotation_sets_map_id_fkey",
        "annotation_sets",
        "maps",
        ["map_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_annotation_sets_map", "annotation_sets", ["map_id"])
