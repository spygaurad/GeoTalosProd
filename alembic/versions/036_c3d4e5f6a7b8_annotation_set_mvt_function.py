"""Add annotation_set_mvt() Postgres function for Martin and extend ck_jobs_type.

Martin (martin_reader role, BYPASSRLS) cannot evaluate the per-request RLS
session variable, so it cannot be safely pointed at the raw ``annotations``
table — that would leak rows across orgs.  Instead we expose a single
parameterised function source.  The FastAPI tile proxy is responsible for
authenticating the request and verifying that the caller's org owns the
``set_id`` before forwarding to Martin.

Also extends ``ck_jobs_type`` to allow the new ``import_annotations`` job
type used by the GeoJSON import worker.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-06 00:00:00.000000
"""

from alembic import op

from app.core.enums import JobType

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


_TYPE_CHECK = "type IN ({})".format(", ".join(f"'{t}'" for t in JobType))


def upgrade() -> None:
    # ── Extend ck_jobs_type to include the new import_annotations type ──
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_type", "jobs", _TYPE_CHECK)

    # ── Martin function source for annotation set tiles ──
    # Filters by set_id passed as a query parameter; returns ST_AsMVT bytes.
    # The proxy enforces tenant isolation; this function does NOT — Martin
    # connects with BYPASSRLS so any policy here would be bypassed anyway.
    op.execute(
        """
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
                    a.annotation_set_id::text AS annotation_set_id
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
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.annotation_set_mvt(integer, integer, integer, json) TO martin_reader"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.annotation_set_mvt(integer, integer, integer, json)"
    )
    op.drop_constraint("ck_jobs_type", "jobs", type_="check")
    # Recreate the original CHECK from migration 022 (only INGEST)
    op.create_check_constraint("ck_jobs_type", "jobs", "type IN ('ingest')")
