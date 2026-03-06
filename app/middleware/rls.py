"""Row-Level Security context setter.

Called inside every request's database transaction (via get_session in deps.py)
to bind the three PostgreSQL session-local variables that RLS policies read:

  app.current_org_id   — internal UUID of the active organization
  app.current_user_id  — Clerk user ID string (sub claim)
  app.current_role     — org role string (org:admin | org:member | org:viewer)

set_config(..., true) is used instead of SET LOCAL :param because asyncpg does
not support parameters in SET LOCAL statements.

The clerk_org_id → UUID resolution result is cached on request.state.org_uuid
so subsequent calls within the same request (e.g. multiple dependencies) only
hit the DB once.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


async def set_rls_context(session: AsyncSession, request: Request) -> None:
    claims = request.state.clerk_claims
    clerk_org_id: str | None = claims.get("org_id")

    # Resolve clerk_org_id → internal UUID, cached per request.
    org_uuid: str = getattr(request.state, "org_uuid", "")
    if not org_uuid and clerk_org_id:
        row = (
            await session.execute(
                text("SELECT id FROM organizations WHERE clerk_org_id = :cid"),
                {"cid": clerk_org_id},
            )
        ).fetchone()
        org_uuid = str(row[0]) if row else ""
        request.state.org_uuid = org_uuid

    await session.execute(
        text(
            "SELECT"
            "  set_config('app.current_org_id',  :org_id,  true),"
            "  set_config('app.current_user_id', :user_id, true),"
            "  set_config('app.current_role',    :role,    true)"
        ),
        {
            "org_id": org_uuid,
            "user_id": claims.get("sub", ""),
            "role": claims.get("org_role", "org:viewer"),
        },
    )
