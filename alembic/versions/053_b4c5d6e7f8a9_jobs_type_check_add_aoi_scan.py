"""Update jobs type check to include aoi_scan.

Adds ``aoi_scan`` (the async AOI similarity-scan job in geoops) to the
``ck_jobs_type`` check constraint. Rebuilt from the ``JobType`` enum so it
stays in sync with the code.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-13
"""

from alembic import op

from app.core.enums import JobType

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))
# Previous set (pre aoi_scan), for downgrade.
_PRIOR_CHECK = "type IN ({})".format(
    ", ".join(f"'{t}'" for t in JobType if t != JobType.AOI_SCAN)
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
