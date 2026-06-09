"""Add extent_4326 + review_status to annotation_sets.

`extent_4326` is a denormalized bounding envelope (geometry) covering all live
annotations of the set. It powers AOI auto-nesting in the map left panel
(client tests set-extent ⊆ AOI geometry) and fly-to without aggregating
geometries on every read. It is maintained by a DB trigger so that *every*
write path stays correct — interactive ORM writes, model-inference inserts,
bulk core inserts (insert(Annotation.__table__)), and analysis nodes.

`review_status` is the workflow facet for the "Group by status" lens:
  raw       — untouched (model output or fresh manual set)
  corrected — a model-sourced set that a human has edited
  verified  — explicitly signed off by a human

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "annotation_sets",
        sa.Column(
            "extent_4326",
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )
    op.add_column(
        "annotation_sets",
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="raw",
        ),
    )
    op.create_check_constraint(
        "ck_annotation_sets_review_status",
        "annotation_sets",
        "review_status IN ('raw', 'corrected', 'verified')",
    )

    # ── Extent maintenance ───────────────────────────────────────────────
    # Full recompute helper (used for UPDATE/DELETE which may shrink extent).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_annotation_set_extent(p_set uuid)
        RETURNS void
        LANGUAGE sql
        AS $$
            UPDATE annotation_sets s
            SET extent_4326 = sub.env
            FROM (
                SELECT ST_Envelope(ST_Collect(a.geometry)) AS env
                FROM annotations a
                WHERE a.annotation_set_id = p_set
                  AND a.deleted_at IS NULL
            ) sub
            WHERE s.id = p_set;
        $$;
        """
    )

    # Row trigger. INSERT expands incrementally (O(1) — keeps bulk imports fast);
    # UPDATE/DELETE fully recompute the affected set(s) since geometry may shrink
    # or a soft-delete (deleted_at) may drop a boundary annotation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION trg_annotation_set_extent()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (TG_OP = 'INSERT') THEN
                UPDATE annotation_sets s
                SET extent_4326 = CASE
                        WHEN s.extent_4326 IS NULL THEN ST_Envelope(NEW.geometry)
                        ELSE ST_Envelope(ST_Collect(s.extent_4326, NEW.geometry))
                    END
                WHERE s.id = NEW.annotation_set_id;
                RETURN NEW;
            ELSIF (TG_OP = 'DELETE') THEN
                PERFORM refresh_annotation_set_extent(OLD.annotation_set_id);
                RETURN OLD;
            ELSE  -- UPDATE
                PERFORM refresh_annotation_set_extent(NEW.annotation_set_id);
                IF NEW.annotation_set_id <> OLD.annotation_set_id THEN
                    PERFORM refresh_annotation_set_extent(OLD.annotation_set_id);
                END IF;
                RETURN NEW;
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER annotations_extent_aiud
        AFTER INSERT OR UPDATE OR DELETE ON annotations
        FOR EACH ROW EXECUTE FUNCTION trg_annotation_set_extent();
        """
    )

    # Backfill existing sets.
    op.execute(
        """
        UPDATE annotation_sets s
        SET extent_4326 = sub.env
        FROM (
            SELECT a.annotation_set_id, ST_Envelope(ST_Collect(a.geometry)) AS env
            FROM annotations a
            WHERE a.deleted_at IS NULL
            GROUP BY a.annotation_set_id
        ) sub
        WHERE s.id = sub.annotation_set_id;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS annotations_extent_aiud ON annotations")
    op.execute("DROP FUNCTION IF EXISTS trg_annotation_set_extent()")
    op.execute("DROP FUNCTION IF EXISTS refresh_annotation_set_extent(uuid)")
    op.drop_constraint("ck_annotation_sets_review_status", "annotation_sets")
    op.drop_column("annotation_sets", "review_status")
    op.drop_column("annotation_sets", "extent_4326")
