"""create styles

Revision ID: 6f708192
Revises: 5e6f7081
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "6f708192"
down_revision = "5e6f7081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "styles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
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
    op.create_index("idx_styles_org", "styles", ["organization_id"])
    op.create_index("idx_styles_type", "styles", ["type"])


def downgrade() -> None:
    op.drop_index("idx_styles_type", table_name="styles")
    op.drop_index("idx_styles_org", table_name="styles")
    op.drop_table("styles")
