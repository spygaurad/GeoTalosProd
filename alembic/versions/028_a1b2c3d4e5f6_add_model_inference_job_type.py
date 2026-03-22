"""Allow model_inference in jobs.type CHECK constraint.

Revision ID: a1b2c3d4e5f6
Revises: 9165194191ce
Create Date: 2026-03-22 00:00:00.000000
"""

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "9165194191ce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_type",
        "jobs",
        "type IN ('ingest', 'model_inference')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_type",
        "jobs",
        "type IN ('ingest')",
    )
