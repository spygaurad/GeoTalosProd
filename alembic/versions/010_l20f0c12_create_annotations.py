"""create annotations tables

Revision ID: l20f0c12
Revises: k10f0c11
Create Date: 2026-03-10 01:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "l20f0c12"
down_revision: Union[str, None] = "k10f0c11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("dataset_item_id", sa.UUID(), nullable=True),
        sa.Column("stac_item_id", sa.String(), nullable=True),
        sa.Column("geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=True),
        sa.Column("pixel_coords", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("label_schema_id", sa.UUID(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("source", sa.String(length=50), server_default="manual", nullable=False),
        sa.Column("model_id", sa.UUID(), nullable=True),
        sa.Column("track_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="draft", nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("parent_version_id", sa.UUID(), nullable=True),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["dataset_item_id"], ["dataset_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["label_schema_id"], ["label_schemas.id"]),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["annotations.id"]),
        sa.ForeignKeyConstraint(["parent_version_id"], ["annotations.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["track_id"], ["tracked_objects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_annotations_org", "annotations", ["organization_id"], unique=False)
    op.create_index("idx_annotations_dataset_item", "annotations", ["dataset_item_id"], unique=False)
    op.create_index("idx_annotations_stac_item", "annotations", ["stac_item_id"], unique=False)
    op.create_index("idx_annotations_geometry", "annotations", ["geometry"], unique=False, postgresql_using="gist")
    op.create_index("idx_annotations_properties", "annotations", ["properties"], unique=False, postgresql_using="gin")
    op.create_index("idx_annotations_track", "annotations", ["track_id"], unique=False)
    op.create_index("idx_annotations_label", "annotations", ["label"], unique=False)
    op.create_index("idx_annotations_is_current", "annotations", ["is_current"], unique=False)
    op.create_index("idx_annotations_status", "annotations", ["status"], unique=False)
    op.create_index("idx_annotations_tags", "annotations", ["tags"], unique=False, postgresql_using="gin")

    op.create_table(
        "annotation_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("annotation_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=True),
        sa.Column("pixel_coords", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("changed_by", sa.UUID(), nullable=True),
        sa.Column("change_type", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["annotation_id"], ["annotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_annotation_versions_annotation", "annotation_versions", ["annotation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_annotation_versions_annotation", table_name="annotation_versions")
    op.drop_table("annotation_versions")
    op.drop_index("idx_annotations_tags", table_name="annotations")
    op.drop_index("idx_annotations_status", table_name="annotations")
    op.drop_index("idx_annotations_is_current", table_name="annotations")
    op.drop_index("idx_annotations_label", table_name="annotations")
    op.drop_index("idx_annotations_track", table_name="annotations")
    op.drop_index("idx_annotations_properties", table_name="annotations")
    op.drop_index("idx_annotations_geometry", table_name="annotations")
    op.drop_index("idx_annotations_stac_item", table_name="annotations")
    op.drop_index("idx_annotations_dataset_item", table_name="annotations")
    op.drop_index("idx_annotations_org", table_name="annotations")
    op.drop_table("annotations")
