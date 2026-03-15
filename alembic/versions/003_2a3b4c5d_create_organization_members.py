"""create organization members

Revision ID: 2a3b4c5d
Revises: 1f2b3c4d
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "2a3b4c5d"
down_revision = "1f2b3c4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_members",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("organization_id", "user_id", name="pk_organization_members"),
    )
    op.create_index("ix_organization_members_org", "organization_members", ["organization_id"])
    op.create_index("ix_organization_members_user", "organization_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_organization_members_user", table_name="organization_members")
    op.drop_index("ix_organization_members_org", table_name="organization_members")
    op.drop_table("organization_members")
