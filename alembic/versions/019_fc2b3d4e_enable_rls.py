"""Enable Row-Level Security on all directly-org-scoped tables (Group A).

Adds ENABLE ROW LEVEL SECURITY and four policies (select / insert / update /
delete) for app_user on every table that carries a direct organization_id
column.  Tables with *derived* org context (maps, annotation_sets,
annotation_classes, map_layers, annotations, job_outputs) are covered by
migration 020 using subquery policies.

System tables (organizations, users) are explicitly excluded — enabling RLS
on organizations would deadlock the clerk_org_id → UUID lookup that runs
before any context variables are set.

Policy expression used throughout:
    organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid

NULLIF guards against the empty-string fallback that is stored when no org is
resolved (unauthenticated or unknown clerk_org_id).  In that case NULLIF
returns NULL, and `org_id = NULL` evaluates to UNKNOWN, which silently filters
every row — exactly the desired behaviour.

celery_worker and martin_reader both carry BYPASSRLS and are unaffected.

Revision ID: fc2b3d4e
Revises: fb1a2c3d
Create Date: 2026-03-15 00:00:00.000000
"""

from alembic import op

revision = "fc2b3d4e"
down_revision = "fb1a2c3d"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Tables with a direct organization_id FK column (Group A).
# Order matches FK dependency so downgrade can reverse safely.
# ---------------------------------------------------------------------------
_GROUP_A_TABLES = [
    "organization_members",
    "projects",
    "datasets",
    "styles",
    "annotation_schemas",
    "ai_models",
    "jobs",
    "activity_logs",
]

_POLICY_EXPR = (
    "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # SELECT — can only read own-org rows
    op.execute(
        f"CREATE POLICY {table}_select ON {table}"
        f"  FOR SELECT TO app_user"
        f"  USING ({_POLICY_EXPR})"
    )

    # INSERT — can only insert into own org
    op.execute(
        f"CREATE POLICY {table}_insert ON {table}"
        f"  FOR INSERT TO app_user"
        f"  WITH CHECK ({_POLICY_EXPR})"
    )

    # UPDATE — can only modify own-org rows, and cannot move a row to another org
    op.execute(
        f"CREATE POLICY {table}_update ON {table}"
        f"  FOR UPDATE TO app_user"
        f"  USING ({_POLICY_EXPR})"
        f"  WITH CHECK ({_POLICY_EXPR})"
    )

    # DELETE — can only delete own-org rows
    op.execute(
        f"CREATE POLICY {table}_delete ON {table}"
        f"  FOR DELETE TO app_user"
        f"  USING ({_POLICY_EXPR})"
    )


def _disable_rls(table: str) -> None:
    for op_name in ("select", "insert", "update", "delete"):
        op.execute(f"DROP POLICY IF EXISTS {table}_{op_name} ON {table}")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    for table in _GROUP_A_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_GROUP_A_TABLES):
        _disable_rls(table)
