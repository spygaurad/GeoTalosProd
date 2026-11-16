"""Update jobs type check to include the conversion job types.

Adds ``convert_to_cog``, ``extract_raster_features`` and ``reproject_raster``
to the ``ck_jobs_type`` check constraint. Rebuilt from the ``JobType`` enum so
it stays in sync with the code.

Revision ID: 9b4e2c7a1f3d
Revises: c2720855bffe
Create Date: 2026-11-10
"""

from alembic import op

from app.core.enums import JobType

revision = "9b4e2c7a1f3d"
down_revision = "c2720855bffe"
branch_labels = None
depends_on = None


_CONVERSION_TYPES = {
    JobType.CONVERT_TO_COG,
    JobType.EXTRACT_RASTER_FEATURES,
    JobType.REPROJECT_RASTER,
}
_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))
# Previous set (without the conversion job types), for downgrade.
_PRIOR_CHECK = "type IN ({})".format(
    ", ".join(f"'{t}'" for t in JobType if t not in _CONVERSION_TYPES)
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
