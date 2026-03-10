"""Create pending_invitations table

Revision ID: g70f0c07
Revises: f60f0c06
Create Date: 2026-03-05 00:01:00

Temporary store for Clerk invitation metadata. Records are written when
organizationInvitation.accepted fires and deleted after organizationMembership.created
is processed. No RLS — the webhook handler writes here without a user context.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "g70f0c07"
down_revision: Union[str, None] = "f60f0c06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_invitations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("clerk_org_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("app_role", sa.String(length=50), nullable=True),
        sa.Column(
            "project_ids",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("invited_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clerk_org_id", "email", name="uq_pending_inv_org_email"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE pending_invitations TO app_user;")


def downgrade() -> None:
    op.drop_table("pending_invitations")
