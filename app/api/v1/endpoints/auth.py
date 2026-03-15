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
from app.models.organization import Organization
from app.models.organization_member import OrganizationMember
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _map_clerk_role(role: str) -> str:
    return {"org:admin": "admin", "org:member": "member"}.get(role, "viewer")


@router.post("/sync")
async def sync_session(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    claims = request.state.clerk_claims
    clerk_id: str = claims["sub"]
    clerk_org_id: str | None = claims.get("org_id")
    clerk_role: str = claims.get("org_role", "org:viewer")
    email: str = claims.get("email", "")
    name: str = claims.get("name", "")

    # ── 1. Upsert user ────────────────────────────────────────────────────────
    await session.execute(
        pg_insert(User.__table__)
        .values(id=uuid.uuid4(), clerk_id=clerk_id, email=email, name=name)
        .on_conflict_do_update(
            index_elements=["clerk_id"],
            set_={"email": email, "name": name},
        )
    )
    user_row = (await session.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one()

    org_row: Organization | None = None

    if clerk_org_id:
        # ── 2. Upsert org ─────────────────────────────────────────────────────
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
            pg_insert(OrganizationMember.__table__)
            .values(
                user_id=user_row.id,
                organization_id=org_row.id,
                role=_map_clerk_role(clerk_role),
            )
            .on_conflict_do_update(
                index_elements=["user_id", "organization_id"],
                set_={"role": _map_clerk_role(clerk_role)},
            )
        )

    await session.commit()

    return {
        "status": "ok",
        "user_id": str(user_row.id),
        "org_id": str(org_row.id) if org_row else None,
    }
