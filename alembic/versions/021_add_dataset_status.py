"""Add status column to datasets table.

Values:  pending | ingesting | ready | failed
Default: pending

The Celery ingest_dataset task transitions:
  pending  → ingesting  (on task start)
  ingesting → ready     (on success)
  ingesting → failed    (on terminal failure)

Revision ID: 0a1b2c3d4e5f
Revises: fd3c4e5f
Create Date: 2026-03-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from app.core.enums import DatasetStatus

revision = "0a1b2c3d4e5f"
down_revision = "fd3c4e5f"
branch_labels = None
depends_on = None

# Build the SQL IN-list from the enum so the constraint and the application
# code are guaranteed to stay in sync.
_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{s}'" for s in DatasetStatus)
)


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_check_constraint(
        "ck_datasets_status",
        "datasets",
        _STATUS_CHECK,
    )
    op.create_index("idx_datasets_status", "datasets", ["status"])


def downgrade() -> None:
    op.drop_index("idx_datasets_status", table_name="datasets")
    op.drop_constraint("ck_datasets_status", "datasets", type_="check")
    op.drop_column("datasets", "status")
