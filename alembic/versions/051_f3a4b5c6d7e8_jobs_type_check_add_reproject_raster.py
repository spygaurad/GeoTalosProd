"""Update jobs type check to include reproject_raster.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-01
"""

from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


_TYPE_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set', "
    "'convert_to_cog', 'extract_raster_features', 'reproject_raster')"
)
_PRIOR_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set', "
    "'convert_to_cog', 'extract_raster_features')"
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
