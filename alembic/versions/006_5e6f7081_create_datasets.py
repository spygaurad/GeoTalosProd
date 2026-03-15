"""create datasets

Revision ID: 5e6f7081
Revises: 4d5e6f70
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "5e6f7081"
down_revision = "4d5e6f70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("dataset_type", sa.String(length=50), nullable=False),
        sa.Column("stac_collection_id", sa.String(length=255), nullable=True),
        sa.Column("geometry", Geometry("POLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("temporal_extent", postgresql.TSTZRANGE(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_datasets_org", "datasets", ["organization_id"])
    op.create_index("idx_datasets_stac_collection", "datasets", ["stac_collection_id"])
    op.create_index(
        "idx_datasets_geometry",
        "datasets",
        ["geometry"],
        postgresql_using="gist",
    )
    op.create_index(
        "idx_datasets_temporal_extent",
        "datasets",
        ["temporal_extent"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("idx_datasets_temporal_extent", table_name="datasets")
    op.drop_index("idx_datasets_geometry", table_name="datasets")
    op.drop_index("idx_datasets_stac_collection", table_name="datasets")
    op.drop_index("idx_datasets_org", table_name="datasets")
    op.drop_table("datasets")
