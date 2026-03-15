"""add unique constraint on organizations.clerk_org_id

Revision ID: fb1a2c3d
Revises: fa091b2c
Create Date: 2026-03-15 00:00:00.000000
"""

from alembic import op

revision = "fb1a2c3d"
down_revision = "fa091b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_organizations_clerk_org_id",
        "organizations",
        ["clerk_org_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_organizations_clerk_org_id", "organizations", type_="unique")
