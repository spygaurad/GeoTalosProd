"""Update jobs type check to include raster conversion job types.

Adds ``rasterize_annotation_set`` (and ``vectorize_raster_mask``, never added
when it was introduced) to the ``ck_jobs_type`` check constraint. The check is
rebuilt from the ``JobType`` enum so it stays in sync with the code.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-04
"""

from alembic import op

from app.core.enums import JobType

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))
# Previous explicit set (pre raster conversion jobs), for downgrade.
_PRIOR_CHECK = "type IN ('ingest', 'inference', 'import_annotations')"


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
