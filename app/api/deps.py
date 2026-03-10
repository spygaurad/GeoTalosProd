"""FastAPI dependency chain for auth, DB session, and RLS.

Dependency order for protected endpoints:

  Request
    └── ClerkAuthMiddleware          (validates JWT → request.state.clerk_claims)
          └── get_session            (opens AsyncSession, sets RLS context)
                └── get_current_user (upserts User from claims, returns ORM object)
                      └── require_role("org:admin")   (optional role guard)
"""

import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.middleware.rls import set_rls_context
from app.models.user import User


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Open a transactional AsyncSession and set RLS context before yielding."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await set_rls_context(session, request)
            yield session


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Upsert the calling user from Clerk JWT claims and return the ORM object."""
    claims = request.state.clerk_claims
    clerk_user_id: str = claims["sub"]
    email: str = claims.get("email", "")
    name: str = claims.get("name", "")

    # Upsert — idempotent on clerk_user_id.
    await session.execute(
        pg_insert(User.__table__)
        .values(id=uuid.uuid4(), clerk_user_id=clerk_user_id, email=email, name=name)
        .on_conflict_do_update(
            index_elements=["clerk_user_id"],
            set_={"email": email, "name": name},
        )
    )

    result = await session.execute(
        select(User).where(User.clerk_user_id == clerk_user_id)
    )
    return result.scalar_one()


async def get_current_org_id(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: ARG001 — ensures RLS is set first
) -> uuid.UUID | None:
    """Return the internal org UUID resolved by set_rls_context, or None."""
    org_uuid: str = getattr(request.state, "org_uuid", "")
    if not org_uuid:
        return None
    return uuid.UUID(org_uuid)


_ROLE_RANK = {"org:viewer": 0, "org:member": 1, "org:admin": 2}


def require_role(min_role: str):
    """Dependency factory — raises 403 if the caller's role is below min_role."""

    async def _check(request: Request):
        role = request.state.clerk_claims.get("org_role", "org:viewer")
        if _ROLE_RANK.get(role, 0) < _ROLE_RANK.get(min_role, 0):
            raise HTTPException(status_code=403, detail="Insufficient permissions")

    return Depends(_check)
