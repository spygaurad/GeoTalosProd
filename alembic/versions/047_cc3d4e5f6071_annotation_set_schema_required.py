"""Stage 1 — make ``annotation_sets.schema_id`` NOT NULL.

Backfill strategy (in order of preference):

1. If the set has a ``model_id`` pointing at an ``ai_models`` row with
   ``annotation_schema_id`` set → adopt that schema.  (Typical path for any
   ML-produced set.)
2. Otherwise create a placeholder schema ``"Legacy — <set_name>"`` owned by
   the same org with ``geometry_types = ['Polygon','Point','LineString']``.

Every backfill is recorded in ``activity_logs`` with action
``'schema_backfilled'``, entity_type ``'annotation_set'``, and metadata
``{source, schema_id}`` so that downgrade can precisely undo the change —
SET NULL only for rows we touched, and DELETE the placeholder schemas we
created.

Revision ID: cc3d4e5f6071
Revises: bb2c3d4e5f60
Create Date: 2026-04-21
"""
from alembic import op

revision = "cc3d4e5f6071"
down_revision = "bb2c3d4e5f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Step 1: map via model.annotation_schema_id ──────────────────────────
    op.execute(
        """
        CREATE TEMP TABLE _model_backfill_candidates AS
        SELECT s.id AS set_id, s.organization_id, m.annotation_schema_id
        FROM annotation_sets s
        JOIN ai_models m ON m.id = s.model_id
        WHERE s.schema_id IS NULL
          AND m.annotation_schema_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE annotation_sets s
        SET schema_id = c.annotation_schema_id
        FROM _model_backfill_candidates c
        WHERE s.id = c.set_id
        """
    )
    op.execute(
        """
        INSERT INTO activity_logs (
            organization_id, action, entity_type, entity_id, metadata, created_at
        )
        SELECT
            c.organization_id,
            'schema_backfilled',
            'annotation_set',
            c.set_id,
            jsonb_build_object(
                'source', 'model_annotation_schema_id',
                'schema_id', c.annotation_schema_id::text,
                'migration', '047'
            ),
            now()
        FROM _model_backfill_candidates c
        """
    )
    op.execute("DROP TABLE _model_backfill_candidates")

    # ── Step 2: placeholder schema for each remaining NULL row ──────────────
    op.execute(
        """
        DO $$
        DECLARE
            set_row RECORD;
            new_schema_id uuid;
        BEGIN
            FOR set_row IN
                SELECT id, organization_id, name
                FROM annotation_sets
                WHERE schema_id IS NULL
            LOOP
                INSERT INTO annotation_schemas (
                    id, organization_id, name, description, version,
                    geometry_types, created_at, updated_at
                )
                VALUES (
                    gen_random_uuid(),
                    set_row.organization_id,
                    'Legacy — '
                        || left(set_row.name, 200)
                        || ' ('
                        || substring(set_row.id::text, 1, 8)
                        || ')',
                    'Auto-generated placeholder schema during migration 047 '
                        || '(annotation_sets.schema_id NOT NULL).',
                    1,
                    ARRAY['Polygon', 'Point', 'LineString'],
                    now(),
                    now()
                )
                RETURNING id INTO new_schema_id;

                UPDATE annotation_sets
                SET schema_id = new_schema_id
                WHERE id = set_row.id;

                INSERT INTO activity_logs (
                    organization_id, action, entity_type, entity_id, metadata, created_at
                )
                VALUES (
                    set_row.organization_id,
                    'schema_backfilled',
                    'annotation_set',
                    set_row.id,
                    jsonb_build_object(
                        'source', 'placeholder',
                        'schema_id', new_schema_id::text,
                        'migration', '047'
                    ),
                    now()
                );
            END LOOP;
        END
        $$;
        """
    )

    # ── Step 3: enforce NOT NULL ────────────────────────────────────────────
    op.alter_column(
        "annotation_sets", "schema_id", nullable=False
    )

    # ── Step 4: recreate FK with ON DELETE RESTRICT ─────────────────────────
    # Schemas carry class semantics for every annotation they scope; deleting
    # a schema that still has annotation sets must be an explicit operation.
    op.drop_constraint(
        "annotation_sets_schema_id_fkey", "annotation_sets", type_="foreignkey"
    )
    op.create_foreign_key(
        "annotation_sets_schema_id_fkey",
        "annotation_sets",
        "annotation_schemas",
        ["schema_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # Restore the original FK (NO ACTION) before relaxing NOT NULL.
    op.drop_constraint(
        "annotation_sets_schema_id_fkey", "annotation_sets", type_="foreignkey"
    )
    op.create_foreign_key(
        "annotation_sets_schema_id_fkey",
        "annotation_sets",
        "annotation_schemas",
        ["schema_id"],
        ["id"],
    )

    # Re-allow NULL so we can restore the original state of touched rows.
    op.alter_column(
        "annotation_sets", "schema_id", nullable=True
    )

    # Set the original NULLs back for every row we backfilled.
    op.execute(
        """
        UPDATE annotation_sets s
        SET schema_id = NULL
        FROM activity_logs l
        WHERE l.action = 'schema_backfilled'
          AND l.entity_type = 'annotation_set'
          AND (l.metadata->>'migration') = '047'
          AND l.entity_id = s.id
          AND s.schema_id = (l.metadata->>'schema_id')::uuid
        """
    )

    # Delete only the placeholder schemas we created in upgrade().
    op.execute(
        """
        DELETE FROM annotation_schemas
        WHERE id IN (
            SELECT (metadata->>'schema_id')::uuid
            FROM activity_logs
            WHERE action = 'schema_backfilled'
              AND entity_type = 'annotation_set'
              AND (metadata->>'migration') = '047'
              AND (metadata->>'source') = 'placeholder'
        )
        """
    )

    # Remove our audit trail so a subsequent upgrade starts clean.
    op.execute(
        """
        DELETE FROM activity_logs
        WHERE action = 'schema_backfilled'
          AND entity_type = 'annotation_set'
          AND (metadata->>'migration') = '047'
        """
    )
