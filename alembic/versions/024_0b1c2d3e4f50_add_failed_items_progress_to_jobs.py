"""Add failed_items and progress columns to jobs table.

Revision ID: 0b1c2d3e4f50
Revises: fe91cf034329
Create Date: 2026-03-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0b1c2d3e4f50"
down_revision = "fe91cf034329"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("jobs", sa.Column("progress", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "progress")
    op.drop_column("jobs", "failed_items")
