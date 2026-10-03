"""Update jobs type check to include extract_raster_features.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-30
"""

from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


_TYPE_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set', "
    "'convert_to_cog', 'extract_raster_features')"
)
_PRIOR_CHECK = (
    "type IN ('ingest', 'inference', 'import_annotations', "
    "'vectorize_raster_mask', 'rasterize_annotation_set', 'convert_to_cog')"
)


def upgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _PRIOR_CHECK)
