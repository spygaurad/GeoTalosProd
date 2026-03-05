"""create org_memberships table

Revision ID: c30f0c03
Revises: b20f0c02
Create Date: 2026-03-04 00:02:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c30f0c03"
down_revision: Union[str, None] = "b20f0c02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_memberships",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=50), server_default="org:viewer", nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('org:admin','org:member','org:viewer')", name="chk_org_memberships_role"),
        sa.CheckConstraint("status IN ('active','invited','suspended')", name="chk_org_memberships_status"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "organization_id"),
    )
    op.create_index("idx_org_memberships_org", "org_memberships", ["organization_id"], unique=False)
    op.create_index("idx_org_memberships_user", "org_memberships", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_org_memberships_user", table_name="org_memberships")
    op.drop_index("idx_org_memberships_org", table_name="org_memberships")
    op.drop_table("org_memberships")
