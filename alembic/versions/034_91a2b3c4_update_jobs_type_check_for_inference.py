"""Update jobs type check to include inference.

Revision ID: 91a2b3c4
Revises: 8091a2b3
Create Date: 2026-04-02
"""

from alembic import op

from app.core.enums import JobType

revision = "91a2b3c4"
down_revision = "8091a2b3"
branch_labels = None
depends_on = None


_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", "type IN ('ingest')")
