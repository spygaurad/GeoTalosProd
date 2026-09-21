"""Update jobs type check to include anomaly_detection.

Adds ``anomaly_detection`` (the async anomaly-detection job in geoops) to the
``ck_jobs_type`` check constraint. Rebuilt from the ``JobType`` enum so it
stays in sync with the code.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-09-14
"""

from alembic import op

from app.core.enums import JobType

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))
# Previous set (pre anomaly_detection), for downgrade.
_PRIOR_CHECK = "type IN ({})".format(
    ", ".join(f"'{t}'" for t in JobType if t != JobType.ANOMALY_DETECTION)
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
