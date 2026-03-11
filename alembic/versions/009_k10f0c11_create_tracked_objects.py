"""create tracked objects table

Revision ID: k10f0c11
Revises: j00f0c10
Create Date: 2026-03-10 05:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "k10f0c11"
down_revision: Union[str, None] = "j00f0c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tracked_objects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("object_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="active", nullable=False),
        sa.Column("priority", sa.String(length=50), server_default="medium", nullable=True),
        sa.Column("severity", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("merged_into_id", sa.UUID(), nullable=True),
        sa.Column("alert_threshold", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latest_geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=True),
        sa.Column("cumulative_geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=True),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["merged_into_id"], ["tracked_objects.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_tracked_objects_org", "tracked_objects", ["organization_id"], unique=False)
    op.create_index(
        "idx_tracked_objects_type_status", "tracked_objects", ["object_type", "status"], unique=False
    )
    op.create_index(
        "idx_tracked_objects_latest_geom",
        "tracked_objects",
        ["latest_geometry"],
        unique=False,
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("idx_tracked_objects_latest_geom", table_name="tracked_objects")
    op.drop_index("idx_tracked_objects_type_status", table_name="tracked_objects")
    op.drop_index("idx_tracked_objects_org", table_name="tracked_objects")
    op.drop_table("tracked_objects")
