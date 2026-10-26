"""Expose review_status in annotation_set_mvt() tiles.

The map styles verified annotations differently and hides "verify" for
already-verified ones. Both read the feature's ``review_status``, which the
verify flow stores in ``annotations.properties``. MVT features only carried
id/class_id/confidence/annotation_set_id, so tile-rendered layers lost that
distinction. This adds ``review_status`` to each tile feature.

Every set's tile_version is advanced so no cached tile (Redis or Martin)
without the new property can be served.

Revision ID: 6534dde1f720
Revises: ebed450e6672
Create Date: 2026-10-04
"""
from alembic import op

revision = "6534dde1f720"
down_revision = "ebed450e6672"
branch_labels = None
depends_on = None


def _create_function(extra_column: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.annotation_set_mvt(
            z integer, x integer, y integer, query_params json
        )
        RETURNS bytea
        AS $$
        DECLARE
            set_id uuid;
            mvt bytea;
        BEGIN
            IF query_params IS NULL OR query_params->>'set_id' IS NULL THEN
                RETURN NULL;
            END IF;
            set_id := (query_params->>'set_id')::uuid;

            SELECT INTO mvt ST_AsMVT(tile, 'annotation_set_mvt', 4096, 'geom')
            FROM (
                SELECT
                    ST_AsMVTGeom(
                        ST_Transform(a.geometry, 3857),
                        ST_TileEnvelope(z, x, y),
                        4096,
                        64,
                        true
                    ) AS geom,
                    a.id::text                AS id,
                    a.class_id::text          AS class_id,
                    a.confidence              AS confidence,
                    a.annotation_set_id::text AS annotation_set_id{extra_column}
                FROM annotations a
                WHERE a.annotation_set_id = set_id
                  AND a.deleted_at IS NULL
                  AND a.geometry && ST_Transform(
                          ST_TileEnvelope(z, x, y),
                          4326
                      )
            ) AS tile;

            RETURN mvt;
        END
        $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        """
    )


def upgrade() -> None:
    _create_function(",\n                    a.properties->>'review_status' AS review_status")
    op.execute("UPDATE annotation_sets SET tile_version = tile_version + 1")


def downgrade() -> None:
    _create_function("")
    op.execute("UPDATE annotation_sets SET tile_version = tile_version + 1")
