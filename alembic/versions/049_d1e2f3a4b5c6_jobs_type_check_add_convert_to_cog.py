"""Update jobs type check to include convert_to_cog.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-09-20
"""

from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


_TYPE_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set', 'convert_to_cog')"
)
_PRIOR_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set')"
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
