"""Auth sync endpoint.

Called by the frontend on every login as a safety net for missed webhooks.
Upserts the calling user, their active organization, and their membership in
one transaction so the first API request after login always succeeds even if
the corresponding webhook events were delayed or dropped.

POST /api/v1/auth/sync
  - Requires a valid Clerk Bearer JWT (enforced by ClerkAuthMiddleware).
  - Idempotent — safe to call multiple times per session.
  - Returns the internal user_id and org_id for the frontend to cache.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/sync")
async def sync_session(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    claims = request.state.clerk_claims
    clerk_user_id: str = claims["sub"]
    clerk_org_id: str | None = claims.get("org_id")
    clerk_role: str = claims.get("org_role", "org:viewer")
    email: str = claims.get("email", "")
    name: str = claims.get("name", "")

    # ── 1. Upsert user ────────────────────────────────────────────────────────
    await session.execute(
        pg_insert(User.__table__)
        .values(id=uuid.uuid4(), clerk_user_id=clerk_user_id, email=email, name=name)
        .on_conflict_do_update(
            index_elements=["clerk_user_id"],
            set_={"email": email, "name": name, "is_active": True},
        )
    )
    user_row = (
        await session.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    ).scalar_one()

    org_row: Organization | None = None

    if clerk_org_id:
        # ── 2. Upsert org ─────────────────────────────────────────────────────
        # Clerk may not send org name/slug in JWT — use clerk_org_id as fallback
        # slug so the row can be created. The organization.created webhook (or a
        # subsequent sync) will fill in the real name/slug.
        fallback_slug = f"org-{clerk_org_id}"
        await session.execute(
            pg_insert(Organization.__table__)
            .values(
                id=uuid.uuid4(),
                clerk_org_id=clerk_org_id,
                name=clerk_org_id,   # placeholder until organization.created fires
                slug=fallback_slug,
            )
            .on_conflict_do_nothing(index_elements=["clerk_org_id"])
        )
        org_row = (
            await session.execute(
                select(Organization).where(Organization.clerk_org_id == clerk_org_id)
            )
        ).scalar_one()

        # ── 3. Upsert membership ──────────────────────────────────────────────
        await session.execute(
            pg_insert(OrgMembership.__table__)
            .values(
                user_id=user_row.id,
                organization_id=org_row.id,
                role=clerk_role,
                status="active",
            )
            .on_conflict_do_update(
                index_elements=["user_id", "organization_id"],
                set_={"role": clerk_role, "status": "active"},
            )
        )

    return {
        "status": "ok",
        "user_id": str(user_row.id),
        "org_id": str(org_row.id) if org_row else None,
    }
