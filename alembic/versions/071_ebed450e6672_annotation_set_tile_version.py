"""Add annotation_sets.tile_version, advanced by a DB trigger on annotation writes.

`tile_version` is the cache namespace for an annotation set's vector tiles:
tile:{org_id}:{set_id}:{tile_version}:{z}:{x}:{y}. Any committed write to the
set's annotations advances it, so cached tiles for the old version become
unreachable without purging. It is not a count of user edits.

Maintained in the database (like extent_4326, migration 047) so every write
path is covered: interactive ORM writes, bulk core inserts, inference,
conversion and analysis nodes. Triggers are statement-level with transition
tables, so a bulk statement advances each affected set once, not once per row.
An update that moves annotations advances both the old and the new set.
The increment runs in the writer's transaction, so a rollback undoes it and
the row lock on annotation_sets serializes concurrent increments.

Revision ID: ebed450e6672
Revises: 7c3e91d2b45a
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "ebed450e6672"
down_revision = "7c3e91d2b45a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "annotation_sets",
        sa.Column("tile_version", sa.BigInteger(), nullable=False, server_default="1"),
    )

    # SECURITY DEFINER: the bump must not be skipped by RLS on annotation_sets.
    # It only touches sets referenced by rows the caller just wrote.
    # FOR NO KEY UPDATE, not FOR UPDATE: an INSERT holds a KEY SHARE lock on its
    # annotation_sets row (the foreign key), and FOR UPDATE conflicts with it, so two
    # writers into one set each waited on the other and deadlocked. NO KEY UPDATE
    # still serializes writers but does not conflict with KEY SHARE.
    # ponytail: any UPDATE bumps, including property-only edits the tile does
    # not render; filter on rendered columns if needless cache misses matter.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION trg_annotation_set_tile_version()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE annotation_sets SET tile_version = tile_version + 1
                WHERE id IN (
                    SELECT s.id FROM annotation_sets s
                    WHERE s.id IN (SELECT annotation_set_id FROM new_rows)
                    ORDER BY s.id FOR NO KEY UPDATE
                );
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE annotation_sets SET tile_version = tile_version + 1
                WHERE id IN (
                    SELECT s.id FROM annotation_sets s
                    WHERE s.id IN (SELECT annotation_set_id FROM old_rows)
                    ORDER BY s.id FOR NO KEY UPDATE
                );
            ELSE  -- UPDATE: old and new sets both change when annotations move
                UPDATE annotation_sets SET tile_version = tile_version + 1
                WHERE id IN (
                    SELECT s.id FROM annotation_sets s
                    WHERE s.id IN (
                        SELECT annotation_set_id FROM old_rows
                        UNION
                        SELECT annotation_set_id FROM new_rows
                    )
                    ORDER BY s.id FOR NO KEY UPDATE
                );
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    # One trigger per event: transition tables differ by event.
    op.execute(
        """
        CREATE TRIGGER annotations_tile_version_ins
        AFTER INSERT ON annotations
        REFERENCING NEW TABLE AS new_rows
        FOR EACH STATEMENT EXECUTE FUNCTION trg_annotation_set_tile_version();
        """
    )
    op.execute(
        """
        CREATE TRIGGER annotations_tile_version_upd
        AFTER UPDATE ON annotations
        REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
        FOR EACH STATEMENT EXECUTE FUNCTION trg_annotation_set_tile_version();
        """
    )
    op.execute(
        """
        CREATE TRIGGER annotations_tile_version_del
        AFTER DELETE ON annotations
        REFERENCING OLD TABLE AS old_rows
        FOR EACH STATEMENT EXECUTE FUNCTION trg_annotation_set_tile_version();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS annotations_tile_version_del ON annotations")
    op.execute("DROP TRIGGER IF EXISTS annotations_tile_version_upd ON annotations")
    op.execute("DROP TRIGGER IF EXISTS annotations_tile_version_ins ON annotations")
    op.execute("DROP FUNCTION IF EXISTS trg_annotation_set_tile_version()")
    op.drop_column("annotation_sets", "tile_version")
