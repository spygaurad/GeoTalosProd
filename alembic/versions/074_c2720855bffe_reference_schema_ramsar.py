"""Add the reference schema and the Ramsar table served live by Martin.

Small public reference layers live in PostGIS, in their own schema so they never
mix with tenant tables (no RLS, global and read-only). Ramsar is the first one.
The table is created empty with its spatial indexes; scripts/reference_layers/
load_ramsar.sh fills it. Martin serves it through reference.ramsar_mvt().

Two geometry columns: geom (full detail) and geom_s (simplified, about 1 km),
used at zoom 6 and below so a world tile does not clip nine million vertices.

Large layers do not go here: they are Tippecanoe PMTiles files in the MinIO
reference-tiles bucket (see scripts/reference_layers/README.md).

Revision ID: c2720855bffe
Revises: c7aa1525a337
Create Date: 2026-10-26
"""
from alembic import op

revision = "c2720855bffe"
down_revision = "c7aa1525a337"
branch_labels = None
depends_on = None

_TILEJSON = (
    '{"name": "Ramsar Sites", "description": "Wetlands of International Importance, published boundaries", '
    '"attribution": "<a href=\\"https://rsis.ramsar.org\\">Ramsar Sites © Convention on Wetlands, RSIS</a> '
    '(<a href=\\"https://creativecommons.org/licenses/by/4.0/\\">CC BY 4.0</a>), modified", '
    '"minzoom": 0, "maxzoom": 14}'
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS reference")
    op.execute(
        """
        CREATE TABLE reference.ramsar (
            id          bigserial PRIMARY KEY,
            ramsarid    integer,
            officialna  text,
            iso3        text,
            country_en  text,
            area_off    double precision,
            geom        geometry(MultiPolygon, 4326) NOT NULL,
            geom_s      geometry(MultiPolygon, 4326)
        )
        """
    )
    op.execute("CREATE INDEX ramsar_geom_gist ON reference.ramsar USING gist (geom)")
    op.execute("CREATE INDEX ramsar_geom_s_gist ON reference.ramsar USING gist (geom_s)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reference.ramsar_mvt(
            z integer, x integer, y integer, query_params json DEFAULT NULL
        )
        RETURNS bytea
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT ST_AsMVT(tile, 'ramsar', 4096, 'geom')
            FROM (
                SELECT
                    ST_AsMVTGeom(
                        ST_Transform(CASE WHEN z <= 6 THEN geom_s ELSE geom END, 3857),
                        ST_TileEnvelope(z, x, y), 4096, 64, true
                    ) AS geom,
                    ramsarid, officialna, iso3, country_en, area_off
                FROM reference.ramsar
                WHERE (CASE WHEN z <= 6 THEN geom_s ELSE geom END)
                      && ST_Transform(ST_TileEnvelope(z, x, y), 4326)
            ) AS tile
            WHERE tile.geom IS NOT NULL
        $$
        """
    )
    op.execute(f"COMMENT ON FUNCTION reference.ramsar_mvt(integer, integer, integer, json) IS '{_TILEJSON}'")
    # Martin's read-only role. New tables in the schema are readable too.
    op.execute("GRANT USAGE ON SCHEMA reference TO martin_reader")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA reference TO martin_reader")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA reference GRANT SELECT ON TABLES TO martin_reader")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS reference CASCADE")
