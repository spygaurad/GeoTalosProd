"""Add CHECK constraint on jobs.status and jobs.type.

Migration 011 created jobs.status as VARCHAR(50) with no constraint, so any
string could be inserted.  This migration tightens it to the canonical values
defined in app.core.enums.

Imports the enums module directly so the constraint values always match the
application code — the same pattern used in migration 021 for datasets.status.

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f
Create Date: 2026-03-16 00:00:00.000000
"""

from alembic import op

from app.core.enums import JobStatus, JobType

revision = "1b2c3d4e5f60"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None

_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{s}'" for s in JobStatus)
)
_TYPE_CHECK = "type IN ({})".format(
    ", ".join(f"'{t}'" for t in JobType)
)


def upgrade() -> None:
    op.create_check_constraint("ck_jobs_status", "jobs", _STATUS_CHECK)
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
