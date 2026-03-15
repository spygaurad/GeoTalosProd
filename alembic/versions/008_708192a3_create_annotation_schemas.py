"""create annotation schemas

Revision ID: 708192a3
Revises: 6f708192
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "708192a3"
down_revision = "6f708192"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotation_schemas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("geometry_types", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("properties_schema", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            "version",
            name="uq_annotation_schemas_org_name_version",
        ),
    )
    op.create_index("idx_annotation_schemas_org", "annotation_schemas", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_annotation_schemas_org", table_name="annotation_schemas")
    op.drop_table("annotation_schemas")
