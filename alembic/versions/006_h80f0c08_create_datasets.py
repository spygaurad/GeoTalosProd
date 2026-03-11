"""create datasets tables

Revision ID: h80f0c08
Revises: e50f0c05
Create Date: 2026-03-09 07:11:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "h80f0c08"
down_revision: Union[str, None] = "e50f0c05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("stac_collection_id", sa.String(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("file_format", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("temporal_extent_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("temporal_extent_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spatial_extent", Geometry(geometry_type="POLYGON", srid=4326), nullable=True),
        sa.Column("license", sa.String(), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("parent_dataset_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_dataset_id"], ["datasets.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_datasets_org", "datasets", ["organization_id"], unique=False)
    op.create_index("idx_datasets_project", "datasets", ["project_id"], unique=False)
    op.create_index("idx_datasets_status", "datasets", ["status"], unique=False)
    op.create_index(
        "idx_datasets_spatial_extent", "datasets", ["spatial_extent"], unique=False, postgresql_using="gist"
    )
    op.create_index("idx_datasets_tags", "datasets", ["tags"], unique=False, postgresql_using="gin")

    op.create_table(
        "dataset_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("stac_item_id", sa.String(), nullable=False),
        sa.Column("stac_collection_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=True),
        sa.Column("datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("properties_cache", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_dataset_items_dataset", "dataset_items", ["dataset_id"], unique=False)
    op.create_index("idx_dataset_items_stac_item", "dataset_items", ["stac_item_id"], unique=False)
    op.create_index("idx_dataset_items_org", "dataset_items", ["organization_id"], unique=False)
    op.create_index(
        "idx_dataset_items_geometry", "dataset_items", ["geometry"], unique=False, postgresql_using="gist"
    )
    op.create_index("idx_dataset_items_datetime", "dataset_items", ["datetime"], unique=False)

    op.create_table(
        "dataset_relationships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("source_dataset_id", sa.UUID(), nullable=False),
        sa.Column("target_dataset_id", sa.UUID(), nullable=False),
        sa.Column("relationship_type", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["source_dataset_id"], ["datasets.id"]),
        sa.ForeignKeyConstraint(["target_dataset_id"], ["datasets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_dataset_relationships_org", "dataset_relationships", ["organization_id"], unique=False
    )
    op.create_index(
        "idx_dataset_relationships_source", "dataset_relationships", ["source_dataset_id"], unique=False
    )
    op.create_index(
        "idx_dataset_relationships_target", "dataset_relationships", ["target_dataset_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_dataset_relationships_target", table_name="dataset_relationships")
    op.drop_index("idx_dataset_relationships_source", table_name="dataset_relationships")
    op.drop_index("idx_dataset_relationships_org", table_name="dataset_relationships")
    op.drop_table("dataset_relationships")
    op.drop_index("idx_dataset_items_datetime", table_name="dataset_items")
    op.drop_index("idx_dataset_items_geometry", table_name="dataset_items")
    op.drop_index("idx_dataset_items_org", table_name="dataset_items")
    op.drop_index("idx_dataset_items_stac_item", table_name="dataset_items")
    op.drop_index("idx_dataset_items_dataset", table_name="dataset_items")
    op.drop_table("dataset_items")
    op.drop_index("idx_datasets_tags", table_name="datasets")
    op.drop_index("idx_datasets_spatial_extent", table_name="datasets")
    op.drop_index("idx_datasets_status", table_name="datasets")
    op.drop_index("idx_datasets_project", table_name="datasets")
    op.drop_index("idx_datasets_org", table_name="datasets")
    op.drop_table("datasets")
