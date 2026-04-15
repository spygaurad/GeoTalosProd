"""make annotation_sets.map_id nullable for map-independent sets

Annotation sets can now exist without being tied to a map (e.g. sets created
by GeoJSON imports that group annotations by purpose / source / context).

Tenant isolation for annotation_sets is enforced by the RLS policy added in
migration 020, which routes via schema_id -> annotation_schemas.organization_id
(NOT via map_id), so dropping the NOT NULL on map_id has no effect on RLS.

A CHECK constraint is added so a set must always have at least one anchor
(map_id or schema_id) — this prevents fully orphaned rows that the RLS policy
could not see anyway.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-06 00:00:00.000000
"""

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("annotation_sets", "map_id", nullable=True)
    op.create_check_constraint(
        "ck_annotation_sets_anchor",
        "annotation_sets",
        "(map_id IS NOT NULL OR schema_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_annotation_sets_anchor", "annotation_sets", type_="check")
    # Note: downgrade will fail if any rows have NULL map_id.
    op.alter_column("annotation_sets", "map_id", nullable=False)
