"""Add raster_config JSONB column to annotation_sets.

Stores the persisted raster mask configuration (colormap, stac references,
band index) directly on the annotation set so it can be retrieved without
requiring a MapLayer record.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'annotation_sets',
        sa.Column('raster_config', JSONB, nullable=True),
    )

    # Public helper used by the unauthenticated tile proxy endpoint.
    #
    # Raster-mask tiles are loaded directly by map libraries (Leaflet/MapLibre)
    # which cannot attach Authorization headers to image <src> requests, so
    # the /tiles/raster-masks/ endpoint is exempt from ClerkAuth.  That means
    # no RLS session variables are set and a plain app_user query would return
    # nothing due to RLS row filtering.
    #
    # This SECURITY DEFINER function runs as its owner (postgres) and bypasses
    # RLS — but it is deliberately narrow: it only returns the raster_config
    # JSONB for a given annotation_set UUID.  No org, user, or geometry data
    # is exposed.  The UUID itself is the capability token (128-bit random,
    # equivalent to a STAC item ID used by titiler without auth).
    op.execute("""
        CREATE OR REPLACE FUNCTION get_raster_config_public(p_annotation_set_id uuid)
        RETURNS jsonb
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT raster_config
            FROM annotation_sets
            WHERE id = p_annotation_set_id
              AND deleted_at IS NULL;
        $$;

        GRANT EXECUTE ON FUNCTION get_raster_config_public(uuid) TO app_user;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS get_raster_config_public(uuid)")
    op.drop_column('annotation_sets', 'raster_config')
