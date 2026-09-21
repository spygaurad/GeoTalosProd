"""Add annotations.dataset_item_id.

Stamps each annotation with the dataset_item (image/COG) its geometry was
drawn/detected on. Previously this was only inferable via the parent
annotation_set's dataset_item_id — which is NULL for dataset-wide sets, most
notably the per-(map, schema) verified set that annotation_service
.verify_annotation() moves human-reviewed annotations into. Without a
per-annotation link, there is no reliable way to know which image to crop a
training patch from once an annotation lives in one of those sets.

Nullable and not backfilled: existing rows predate this column and there's no
non-ambiguous way to backfill dataset-wide sets after the fact (that's the
same problem this column exists to avoid). New annotation-creation paths are
updated in the same change to stamp it going forward.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "annotations",
        sa.Column("dataset_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_annotations_dataset_item",
        "annotations",
        "dataset_items",
        ["dataset_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_annotations_dataset_item", "annotations", ["dataset_item_id"])


def downgrade() -> None:
    op.drop_index("idx_annotations_dataset_item", table_name="annotations")
    op.drop_constraint("fk_annotations_dataset_item", "annotations", type_="foreignkey")
    op.drop_column("annotations", "dataset_item_id")
