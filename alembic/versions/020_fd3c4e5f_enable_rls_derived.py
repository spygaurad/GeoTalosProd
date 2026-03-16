"""Enable Row-Level Security on tables with derived organization context (Group B).

These six tables have no direct organization_id column.  Their organization
context is derived through FK chains.  RLS policies use subqueries that
resolve to the same NULLIF(...) expression used in Group A (migration 019).

Each subquery targets the shortest FK path to an already-secured table that
carries a direct organization_id column, avoiding unnecessary JOIN depth:

  maps            → project_id  → projects.organization_id
  annotation_classes → schema_id → annotation_schemas.organization_id
  annotation_sets → schema_id  → annotation_schemas.organization_id
  annotations     → annotation_set_id → annotation_sets → above chain
  map_layers      → map_id     → maps → projects.organization_id
  job_outputs     → job_id     → jobs.organization_id

Subquery policies are slightly heavier than direct equality checks.  The
planner folds them into index scans on the parent table's PK, so the
overhead is one extra index lookup per row evaluated.  All parent tables
(projects, annotation_schemas, jobs, maps) are indexed on organization_id
or primary key, keeping the cost bounded.

celery_worker and martin_reader carry BYPASSRLS and are unaffected.

Revision ID: fd3c4e5f
Revises: fc2b3d4e
Create Date: 2026-03-15 00:00:00.000000
"""

from alembic import op

revision = "fd3c4e5f"
down_revision = "fc2b3d4e"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Shared helper — the org UUID the current session is scoped to.
# Produces NULL when the session variable is unset (unauthenticated requests)
# which makes every row comparison evaluate to UNKNOWN → row is filtered.
# ---------------------------------------------------------------------------
_ORG_UUID = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

# ---------------------------------------------------------------------------
# Per-table USING expressions (SELECT / UPDATE-USING / DELETE-USING).
# WITH CHECK expressions are the same for every table that has them.
# ---------------------------------------------------------------------------
_USING: dict[str, str] = {
    "maps": (
        f"project_id IN ("
        f"  SELECT id FROM projects WHERE organization_id = {_ORG_UUID}"
        f")"
    ),
    "annotation_classes": (
        f"schema_id IN ("
        f"  SELECT id FROM annotation_schemas WHERE organization_id = {_ORG_UUID}"
        f")"
    ),
    "annotation_sets": (
        # schema_id is NOT NULL — shortest and most stable path to org.
        f"schema_id IN ("
        f"  SELECT id FROM annotation_schemas WHERE organization_id = {_ORG_UUID}"
        f")"
    ),
    "annotations": (
        # Two-hop: annotation_set → annotation_schemas (via schema_id).
        f"annotation_set_id IN ("
        f"  SELECT id FROM annotation_sets"
        f"  WHERE schema_id IN ("
        f"    SELECT id FROM annotation_schemas WHERE organization_id = {_ORG_UUID}"
        f"  )"
        f")"
    ),
    "map_layers": (
        # map_id is NOT NULL; resolve through maps → projects.
        f"map_id IN ("
        f"  SELECT m.id FROM maps m"
        f"  JOIN projects p ON p.id = m.project_id"
        f"  WHERE p.organization_id = {_ORG_UUID}"
        f")"
    ),
    "job_outputs": (
        f"job_id IN ("
        f"  SELECT id FROM jobs WHERE organization_id = {_ORG_UUID}"
        f")"
    ),
}

# Tables where INSERT rows must also satisfy the USING check.
# (All Group B tables that app_user can INSERT into.)
_WITH_CHECK_SAME_AS_USING = {
    "maps",
    "annotation_classes",
    "annotation_sets",
    "annotations",
    "map_layers",
    "job_outputs",
}

# job_outputs has no UPDATE path in the application (append-only), but we
# add the policy anyway for defence-in-depth.


def _enable_rls(table: str) -> None:
    using_expr = _USING[table]

    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    op.execute(
        f"CREATE POLICY {table}_select ON {table}"
        f"  FOR SELECT TO app_user"
        f"  USING ({using_expr})"
    )

    if table in _WITH_CHECK_SAME_AS_USING:
        op.execute(
            f"CREATE POLICY {table}_insert ON {table}"
            f"  FOR INSERT TO app_user"
            f"  WITH CHECK ({using_expr})"
        )

    op.execute(
        f"CREATE POLICY {table}_update ON {table}"
        f"  FOR UPDATE TO app_user"
        f"  USING ({using_expr})"
        f"  WITH CHECK ({using_expr})"
    )

    op.execute(
        f"CREATE POLICY {table}_delete ON {table}"
        f"  FOR DELETE TO app_user"
        f"  USING ({using_expr})"
    )


def _disable_rls(table: str) -> None:
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(f"DROP POLICY IF EXISTS {table}_{op_name} ON {table}")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


# Enable in FK-dependency order so that any cascading effects resolve
# correctly (parent tables are secured before their children).
_ORDERED = [
    "maps",              # depends on projects (already secured in 019)
    "annotation_classes",  # depends on annotation_schemas (secured in 019)
    "annotation_sets",   # depends on annotation_schemas (secured in 019)
    "annotations",       # depends on annotation_sets (secured above)
    "map_layers",        # depends on maps (secured above)
    "job_outputs",       # depends on jobs (secured in 019)
]


def upgrade() -> None:
    for table in _ORDERED:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_ORDERED):
        _disable_rls(table)
