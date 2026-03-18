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

import uuid as _uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

_CLERK_ROLE_MAP = {"admin": "org:admin", "member": "org:member", "viewer": "org:viewer"}


def _map_clerk_role(claims: dict) -> str:
    """Resolve org role from both Clerk JWT v1 (org_role) and v2 (o.rol) formats."""
    raw = claims.get("org_role") or claims.get("o", {}).get("rol", "")
    if raw.startswith("org:"):
        return raw
    return _CLERK_ROLE_MAP.get(raw, "org:viewer")


async def set_rls_context(session: AsyncSession, request: Request) -> None:
    claims = request.state.clerk_claims
    # Support both Clerk JWT v1 (flat: org_id) and v2 (nested: o.id).
    clerk_org_id: str | None = claims.get("org_id") or claims.get("o", {}).get("id")

    # Resolve clerk_org_id → internal UUID, cached per request.
    org_uuid: str = getattr(request.state, "org_uuid", "")
    if not org_uuid and clerk_org_id:
        row = (
            await session.execute(
                text("SELECT id FROM organizations WHERE clerk_org_id = :cid"),
                {"cid": clerk_org_id},
            )
        ).fetchone()

        if row is None:
            # Org not in DB yet (first login after fresh deploy, or missed webhook).
            # Auto-bootstrap from JWT claims so the very first API request works
            # without requiring a separate /auth/sync call.
            # name/slug are placeholders; the Clerk organization.created webhook
            # will overwrite them with real values.
            from app.models.organization import Organization  # lazy — avoids circular import
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            fallback_slug = f"org-{clerk_org_id}"[:255]
            await session.execute(
                pg_insert(Organization.__table__)
                .values(
                    id=_uuid.uuid4(),
                    clerk_org_id=clerk_org_id,
                    name=clerk_org_id,
                    slug=fallback_slug,
                )
                .on_conflict_do_nothing(index_elements=["clerk_org_id"])
            )
            # Re-fetch — a concurrent request may have won the race.
            row = (
                await session.execute(
                    text("SELECT id FROM organizations WHERE clerk_org_id = :cid"),
                    {"cid": clerk_org_id},
                )
            ).fetchone()

        # Use empty string (not nil UUID) when we still can't resolve the org so
        # that get_current_org_id() returns None and require_org_role() raises 403
        # instead of proceeding with a nil UUID and hitting FK violations.
        org_uuid = str(row[0]) if row else ""
        request.state.org_uuid = org_uuid

    # Use is_local=false (session-level) so the values survive transaction
    # boundaries within the same connection.  get_current_user() commits the
    # session mid-request (before the handler body runs); with is_local=true
    # the context would be wiped out in the new transaction and RLS would
    # silently filter every tenant table row.  Session-level values are safe
    # because set_rls_context() is called at the start of every request and
    # always overwrites whatever the previous request left behind.
    await session.execute(
        text(
            "SELECT"
            "  set_config('app.current_org_id',  :org_id,  false),"
            "  set_config('app.current_user_id', :user_id, false),"
            "  set_config('app.current_role',    :role,    false)"
        ),
        {
            "org_id": org_uuid,
            "user_id": claims.get("sub", ""),
            "role": _map_clerk_role(claims),
        },
    )
