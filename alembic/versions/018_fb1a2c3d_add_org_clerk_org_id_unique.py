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
    # Migration 002 already creates this constraint via create_table().
    # This migration was a backfill for databases that predated 002's
    # UniqueConstraint — on a fresh install it must be skipped.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_organizations_clerk_org_id'
            ) THEN
                ALTER TABLE organizations
                    ADD CONSTRAINT uq_organizations_clerk_org_id UNIQUE (clerk_org_id);
            END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_organizations_clerk_org_id", "organizations", type_="unique")
