"""Make annotation_sets.schema_id nullable to support freehand drawing sets.

Revision ID: 2d3e4f5a6b7c
Revises: 1c2d3e4f5a6b
Create Date: 2026-03-17 00:00:00.000000
"""

from alembic import op

revision = "2d3e4f5a6b7c"
down_revision = "1c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("annotation_sets", "schema_id", nullable=True)


def downgrade() -> None:
    # Rows with NULL schema_id will violate the NOT NULL constraint on downgrade.
    # Clean those up before reversing if needed.
    op.alter_column("annotation_sets", "schema_id", nullable=False)
