"""Serve uploaded vector layers (feature_layers) as tiles through Martin.

Same pattern as annotation_set_mvt(): Martin runs as martin_reader (BYPASSRLS), so this
function filters by the organization and layer name that the FastAPI proxy passes after it
has authenticated the caller. The proxy takes the organization from the session, never from
the request. The properties JSONB column is flattened into tile attributes by PostGIS.
With points=true it returns one point per feature (ST_PointOnSurface) with the value of the
`weight` property as `w`, which is what the UI heatmap reads. Non numeric values count as 1.

Also adds the indexes the tiles need: a spatial index on geometry and (organization, layer).

Revision ID: e2a9b6d4c1f7
Revises: d1f4a8c2b7e5
Create Date: 2026-11-23
"""
from alembic import op

revision = "e2a9b6d4c1f7"
down_revision = "d1f4a8c2b7e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS idx_feature_layers_geom ON feature_layers USING gist (geometry)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_feature_layers_org_layer ON feature_layers (organization_id, layer_name)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.feature_layer_mvt(
            z integer, x integer, y integer, query_params json
        )
        RETURNS bytea
        AS $$
        DECLARE
            org uuid;
            layer text;
            as_points boolean;
            weight text;
            mvt bytea;
        BEGIN
            IF query_params IS NULL OR query_params->>'org_id' IS NULL OR query_params->>'layer' IS NULL THEN
                RETURN NULL;
            END IF;
            org := (query_params->>'org_id')::uuid;
            layer := query_params->>'layer';
            as_points := coalesce(query_params->>'points', 'false') = 'true';
            weight := query_params->>'weight';

            IF as_points THEN
                SELECT INTO mvt ST_AsMVT(tile, 'feature_layer_mvt', 4096, 'geom')
                FROM (
                    SELECT
                        ST_AsMVTGeom(
                            ST_Transform(ST_PointOnSurface(f.geometry), 3857),
                            ST_TileEnvelope(z, x, y),
                            4096,
                            0,  -- no buffer: the heatmap draws neighbour tiles itself
                            true
                        ) AS geom,
                        CASE WHEN weight IS NOT NULL AND jsonb_typeof(f.properties->weight) = 'number'
                             THEN (f.properties->>weight)::float8 ELSE 1 END AS w
                    FROM feature_layers f
                    WHERE f.organization_id = org
                      AND f.layer_name = layer
                      AND f.geometry && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
                ) AS tile;
                RETURN mvt;
            END IF;

            SELECT INTO mvt ST_AsMVT(tile, 'feature_layer_mvt', 4096, 'geom')
            FROM (
                SELECT
                    ST_AsMVTGeom(
                        ST_Transform(f.geometry, 3857),
                        ST_TileEnvelope(z, x, y),
                        4096,
                        64,
                        true
                    ) AS geom,
                    f.properties AS properties
                FROM feature_layers f
                WHERE f.organization_id = org
                  AND f.layer_name = layer
                  AND f.geometry && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
            ) AS tile;

            RETURN mvt;
        END
        $$ LANGUAGE plpgsql STABLE PARALLEL SAFE;
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.feature_layer_mvt(integer, integer, integer, json) TO martin_reader"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.feature_layer_mvt(integer, integer, integer, json)")
    op.execute("DROP INDEX IF EXISTS idx_feature_layers_org_layer")
    op.execute("DROP INDEX IF EXISTS idx_feature_layers_geom")
