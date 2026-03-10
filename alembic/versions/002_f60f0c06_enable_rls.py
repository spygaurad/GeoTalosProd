"""Enable Row Level Security on tenant-scoped tables

Revision ID: f60f0c06
Revises: e50f0c05
Create Date: 2026-03-05 00:00:00

RLS is applied to `projects` only. The tables `organizations`, `users`,
`org_memberships`, and `project_members` are intentionally RLS-free — they
are lookup/join tables that the policy engine itself reads to make access
decisions, so protecting them with RLS would create circular dependency.

FORCE ROW LEVEL SECURITY is required because alembic (which creates the
tables) runs as `app_user`, making it the table owner. Without FORCE, owners
bypass RLS. DDL (CREATE/ALTER) is not subject to FORCE RLS — only DML is,
so migrations are unaffected.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f60f0c06"
down_revision: Union[str, None] = "e50f0c05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Grant DML on all core tables to app_user ─────────────────────────────
    # Ensures app_user can perform reads and writes even after RLS is applied.
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON TABLE organizations, users, org_memberships, projects, project_members
          TO app_user;
        """
    )

    # ── Enable RLS on projects ────────────────────────────────────────────────
    op.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE projects FORCE ROW LEVEL SECURITY;")

    # SELECT — any member of the org can read its projects
    op.execute(
        """
        CREATE POLICY projects_select ON projects
          FOR SELECT TO app_user
          USING (
            current_setting('app.current_org_id', true) IS NOT NULL
            AND current_setting('app.current_org_id', true) <> ''
            AND organization_id = current_setting('app.current_org_id', true)::uuid
          );
        """
    )

    # INSERT — org:admin and org:member can create projects
    op.execute(
        """
        CREATE POLICY projects_insert ON projects
          FOR INSERT TO app_user
          WITH CHECK (
            current_setting('app.current_org_id', true) IS NOT NULL
            AND current_setting('app.current_org_id', true) <> ''
            AND organization_id = current_setting('app.current_org_id', true)::uuid
            AND current_setting('app.current_role', true) IN ('org:admin', 'org:member')
          );
        """
    )

    # UPDATE — org:admin and org:member can update projects
    op.execute(
        """
        CREATE POLICY projects_update ON projects
          FOR UPDATE TO app_user
          USING (
            current_setting('app.current_org_id', true) IS NOT NULL
            AND current_setting('app.current_org_id', true) <> ''
            AND organization_id = current_setting('app.current_org_id', true)::uuid
          )
          WITH CHECK (
            current_setting('app.current_org_id', true) IS NOT NULL
            AND current_setting('app.current_org_id', true) <> ''
            AND organization_id = current_setting('app.current_org_id', true)::uuid
            AND current_setting('app.current_role', true) IN ('org:admin', 'org:member')
          );
        """
    )

    # DELETE — org:admin only
    op.execute(
        """
        CREATE POLICY projects_delete ON projects
          FOR DELETE TO app_user
          USING (
            current_setting('app.current_org_id', true) IS NOT NULL
            AND current_setting('app.current_org_id', true) <> ''
            AND organization_id = current_setting('app.current_org_id', true)::uuid
            AND current_setting('app.current_role', true) = 'org:admin'
          );
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS projects_delete ON projects;")
    op.execute("DROP POLICY IF EXISTS projects_update ON projects;")
    op.execute("DROP POLICY IF EXISTS projects_insert ON projects;")
    op.execute("DROP POLICY IF EXISTS projects_select ON projects;")
    op.execute("ALTER TABLE projects NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE projects DISABLE ROW LEVEL SECURITY;")
