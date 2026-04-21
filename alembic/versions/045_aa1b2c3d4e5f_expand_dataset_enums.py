"""Expand DatasetType + add DatasetItem columns for Stage 1 (unified platform plan).

- ``datasets.dataset_type``: add CHECK constraint covering all 5 canonical
  values plus the ``segmentation_mask`` legacy alias.
- ``dataset_items``:
    * ``item_type``  — varchar(30), default ``'imagery'``, CHECK on 4 values.
    * ``derived_from_item_id`` — self-FK (nullable, ON DELETE SET NULL).
    * ``derivation_config`` — JSONB for ``{kind, expression, params, ...}``.

Stage 1 exit criteria: columns exist and are writeable. No endpoint
behavioural change. Existing rows get sensible defaults via the backfill
logic below.

Revision ID: aa1b2c3d4e5f
Revises: e5f6a7b8c9d0
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "aa1b2c3d4e5f"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


_DATASET_TYPES = (
    "imagery",
    "continuous",
    "mask",
    "basemap_tiles",
    "external_reference",
    "segmentation_mask",  # legacy alias — kept until a future rename migration
)
_ITEM_TYPES = ("imagery", "continuous", "mask", "external_reference")


def upgrade() -> None:
    # ── datasets.dataset_type CHECK ────────────────────────────────────────
    # Migration 006 created the column without a CHECK — add one now so the
    # DB and the DatasetType enum stay in sync. Legacy rows stored ``'raster'``
    # from an earlier schema; normalise those to ``'imagery'`` before the
    # CHECK lands so existing data stays valid without weakening the taxonomy.
    op.execute(
        """
        UPDATE datasets
        SET dataset_type = 'imagery'
        WHERE dataset_type = 'raster'
        """
    )
    types_sql = ", ".join(f"'{t}'" for t in _DATASET_TYPES)
    op.create_check_constraint(
        "ck_datasets_dataset_type",
        "datasets",
        f"dataset_type IN ({types_sql})",
    )

    # ── dataset_items.item_type ────────────────────────────────────────────
    op.add_column(
        "dataset_items",
        sa.Column(
            "item_type",
            sa.String(length=30),
            nullable=False,
            server_default="imagery",
        ),
    )
    item_types_sql = ", ".join(f"'{t}'" for t in _ITEM_TYPES)
    op.create_check_constraint(
        "ck_dataset_items_item_type",
        "dataset_items",
        f"item_type IN ({item_types_sql})",
    )

    # Backfill item_type from the parent dataset.dataset_type so existing rows
    # carry meaningful values (default of 'imagery' is fine for imagery rows;
    # mask rows should match their parent).
    op.execute(
        """
        UPDATE dataset_items di
        SET item_type = 'mask'
        FROM datasets d
        WHERE di.dataset_id = d.id
          AND d.dataset_type IN ('mask', 'segmentation_mask')
        """
    )

    # ── dataset_items.derived_from_item_id (self-FK) ───────────────────────
    op.add_column(
        "dataset_items",
        sa.Column("derived_from_item_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_dataset_items_derived_from",
        "dataset_items",
        "dataset_items",
        ["derived_from_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_dataset_items_derived_from",
        "dataset_items",
        ["derived_from_item_id"],
    )

    # ── dataset_items.derivation_config ────────────────────────────────────
    op.add_column(
        "dataset_items",
        sa.Column("derivation_config", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset_items", "derivation_config")
    op.drop_index("idx_dataset_items_derived_from", table_name="dataset_items")
    op.drop_constraint(
        "fk_dataset_items_derived_from", "dataset_items", type_="foreignkey"
    )
    op.drop_column("dataset_items", "derived_from_item_id")
    op.drop_constraint(
        "ck_dataset_items_item_type", "dataset_items", type_="check"
    )
    op.drop_column("dataset_items", "item_type")
    op.drop_constraint("ck_datasets_dataset_type", "datasets", type_="check")
