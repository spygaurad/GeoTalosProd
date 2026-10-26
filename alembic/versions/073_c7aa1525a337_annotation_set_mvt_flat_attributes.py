"""Carry all annotation data in annotation_set_mvt() tiles as flat values.

The map UI reads these from tile features instead of fetching the GeoJSON set,
so every feature now includes the annotation columns (dataset_item_id,
created_by_user_id, created_by_job_id, created_at, updated_at) and the scalar
keys the UI reads from ``annotations.properties`` (aoi_id, aoi_name, source,
source_type, verified, model_id, model_name). ``aoi_bbox`` is an array in
properties and tiles cannot hold arrays, so it is sent as a JSON string.

Only scalar values are copied: a non-boolean ``verified`` becomes NULL rather
than failing the whole tile. Timestamps are ISO-8601 UTC strings.

Every set's tile_version is advanced so no cached tile (Redis or Martin)
without the new properties can be served.

Revision ID: c7aa1525a337
Revises: 6534dde1f720
Create Date: 2026-10-26
"""
from alembic import op

revision = "c7aa1525a337"
down_revision = "6534dde1f720"
branch_labels = None
depends_on = None


def _create_function(extra_columns: str) -> None:
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
                    a.annotation_set_id::text AS annotation_set_id,
                    a.properties->>'review_status' AS review_status{extra_columns}
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


_FLAT_COLUMNS = """,
                    a.dataset_item_id::text        AS dataset_item_id,
                    a.created_by_user_id::text     AS created_by_user_id,
                    a.created_by_job_id::text      AS created_by_job_id,
                    to_char(a.created_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS created_at,
                    to_char(a.updated_at AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS updated_at,
                    a.properties->>'aoi_id'        AS aoi_id,
                    a.properties->>'aoi_name'      AS aoi_name,
                    a.properties->>'source'        AS source,
                    a.properties->>'source_type'   AS source_type,
                    CASE WHEN jsonb_typeof(a.properties->'verified') = 'boolean'
                         THEN (a.properties->>'verified')::boolean END AS verified,
                    a.properties->>'model_id'      AS model_id,
                    a.properties->>'model_name'    AS model_name,
                    (a.properties->'aoi_bbox')::text AS aoi_bbox"""


def upgrade() -> None:
    _create_function(_FLAT_COLUMNS)
    op.execute("UPDATE annotation_sets SET tile_version = tile_version + 1")


def downgrade() -> None:
    _create_function("")
    op.execute("UPDATE annotation_sets SET tile_version = tile_version + 1")
