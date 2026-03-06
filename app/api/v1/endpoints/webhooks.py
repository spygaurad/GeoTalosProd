"""Clerk webhook receiver.

Secured by X-Internal-Key header (not a Clerk JWT). This endpoint is NOT on
the public internet — the Next.js frontend verifies the Svix signature and
relays the event here over the internal network.

Handled events:
  user.created / user.updated / user.deleted
  organization.created / organization.updated / organization.deleted
  organizationMembership.created / organizationMembership.updated / organizationMembership.deleted
  organizationInvitation.accepted  (stores metadata for membership handler)

All handlers are idempotent — safe to receive the same event more than once.
"""

import re
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.pending_invitation import PendingInvitation
from app.models.user import User

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── Auth helper ───────────────────────────────────────────────────────────────

def _verify_internal_key(x_internal_key: str | None) -> None:
    if not x_internal_key or x_internal_key != settings.INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Slug helper ───────────────────────────────────────────────────────────────

def _slugify(text_: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text_.lower())
    return re.sub(r"[\s_-]+", "-", s).strip("-")


# ── Entry point ───────────────────────────────────────────────────────────────

@router.post("/clerk")
async def clerk_webhook(
    request: Request,
    x_internal_key: str | None = Header(None),
):
    _verify_internal_key(x_internal_key)

    payload: dict = await request.json()
    event_type: str = payload.get("type", "")
    data: dict = payload.get("data", {})

    handler = _HANDLERS.get(event_type)
    if handler:
        await handler(data)

    return {"status": "ok", "event": event_type}


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_user_created(data: dict) -> None:
    clerk_user_id = data["id"]
    email = _primary_email(data)
    name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(User.__table__)
                .values(id=uuid.uuid4(), clerk_user_id=clerk_user_id, email=email, name=name)
                .on_conflict_do_update(
                    index_elements=["clerk_user_id"],
                    set_={"email": email, "name": name, "is_active": True},
                )
            )


async def _handle_user_updated(data: dict) -> None:
    clerk_user_id = data["id"]
    email = _primary_email(data)
    name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(User.__table__)
                .values(id=uuid.uuid4(), clerk_user_id=clerk_user_id, email=email, name=name)
                .on_conflict_do_update(
                    index_elements=["clerk_user_id"],
                    set_={"email": email, "name": name},
                )
            )


async def _handle_user_deleted(data: dict) -> None:
    clerk_user_id = data["id"]
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE users SET is_active = false"
                    " WHERE clerk_user_id = :cuid"
                ),
                {"cuid": clerk_user_id},
            )


async def _handle_org_created(data: dict) -> None:
    clerk_org_id = data["id"]
    name = data["name"]
    # Clerk provides a slug; fall back to slugifying the name.
    slug = data.get("slug") or _slugify(name)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(Organization.__table__)
                .values(id=uuid.uuid4(), clerk_org_id=clerk_org_id, name=name, slug=slug)
                .on_conflict_do_update(
                    index_elements=["clerk_org_id"],
                    set_={"name": name},
                    # Do not overwrite an existing slug on conflict — slugs are stable.
                )
            )


async def _handle_org_updated(data: dict) -> None:
    clerk_org_id = data["id"]
    name = data["name"]
    slug = data.get("slug")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            if slug:
                await session.execute(
                    text(
                        "UPDATE organizations SET name = :name, slug = :slug"
                        " WHERE clerk_org_id = :coid"
                    ),
                    {"name": name, "slug": slug, "coid": clerk_org_id},
                )
            else:
                await session.execute(
                    text("UPDATE organizations SET name = :name WHERE clerk_org_id = :coid"),
                    {"name": name, "coid": clerk_org_id},
                )


async def _handle_org_deleted(data: dict) -> None:
    # Orgs with data (projects, datasets) cannot be safely hard-deleted.
    # Log the event and leave the record intact — manual cleanup if needed.
    pass


async def _handle_membership_created(data: dict) -> None:
    clerk_user_id = data["public_user_data"]["user_id"]
    clerk_org_id = data["organization"]["id"]
    clerk_role = data["role"]  # "org:admin" | "org:member"
    email = data["public_user_data"].get("identifier", "")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            user = (
                await session.execute(
                    select(User).where(User.clerk_user_id == clerk_user_id)
                )
            ).scalar_one_or_none()

            org = (
                await session.execute(
                    select(Organization).where(Organization.clerk_org_id == clerk_org_id)
                )
            ).scalar_one_or_none()

            if not user or not org:
                # user.created / organization.created should arrive first.
                # If they haven't, the auth/sync endpoint covers this on next login.
                return

            # Check for pending invitation metadata stored by invitation.accepted.
            pending = (
                await session.execute(
                    select(PendingInvitation).where(
                        PendingInvitation.clerk_org_id == clerk_org_id,
                        PendingInvitation.email == email,
                    )
                )
            ).scalar_one_or_none()

            role = (pending.app_role if pending and pending.app_role else clerk_role)

            await session.execute(
                pg_insert(OrgMembership.__table__)
                .values(user_id=user.id, organization_id=org.id, role=role, status="active")
                .on_conflict_do_update(
                    index_elements=["user_id", "organization_id"],
                    set_={"role": role, "status": "active"},
                )
            )

            if pending:
                await session.execute(
                    delete(PendingInvitation).where(PendingInvitation.id == pending.id)
                )


async def _handle_membership_updated(data: dict) -> None:
    clerk_user_id = data["public_user_data"]["user_id"]
    clerk_org_id = data["organization"]["id"]
    clerk_role = data["role"]

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE org_memberships om"
                    "   SET role = :role"
                    "  FROM users u, organizations o"
                    " WHERE om.user_id = u.id"
                    "   AND om.organization_id = o.id"
                    "   AND u.clerk_user_id = :cuid"
                    "   AND o.clerk_org_id = :coid"
                ),
                {"role": clerk_role, "cuid": clerk_user_id, "coid": clerk_org_id},
            )


async def _handle_membership_deleted(data: dict) -> None:
    clerk_user_id = data["public_user_data"]["user_id"]
    clerk_org_id = data["organization"]["id"]

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    "DELETE FROM org_memberships om"
                    "  USING users u, organizations o"
                    " WHERE om.user_id = u.id"
                    "   AND om.organization_id = o.id"
                    "   AND u.clerk_user_id = :cuid"
                    "   AND o.clerk_org_id = :coid"
                ),
                {"cuid": clerk_user_id, "coid": clerk_org_id},
            )


async def _handle_invitation_accepted(data: dict) -> None:
    """Store invitation metadata so _handle_membership_created can apply it."""
    clerk_org_id = data["organization_id"]
    email = data["email_address"]
    meta: dict[str, Any] = data.get("public_metadata", {})

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(PendingInvitation.__table__)
                .values(
                    id=uuid.uuid4(),
                    clerk_org_id=clerk_org_id,
                    email=email,
                    app_role=meta.get("app_role"),
                    project_ids=meta.get("project_ids", []),
                    invited_by=meta.get("invited_by_user_id"),
                )
                .on_conflict_do_update(
                    constraint="uq_pending_inv_org_email",
                    set_={
                        "app_role": meta.get("app_role"),
                        "project_ids": meta.get("project_ids", []),
                        "invited_by": meta.get("invited_by_user_id"),
                    },
                )
            )


# ── Dispatch table ────────────────────────────────────────────────────────────

_HANDLERS = {
    "user.created": _handle_user_created,
    "user.updated": _handle_user_updated,
    "user.deleted": _handle_user_deleted,
    "organization.created": _handle_org_created,
    "organization.updated": _handle_org_updated,
    "organization.deleted": _handle_org_deleted,
    "organizationMembership.created": _handle_membership_created,
    "organizationMembership.updated": _handle_membership_updated,
    "organizationMembership.deleted": _handle_membership_deleted,
    "organizationInvitation.accepted": _handle_invitation_accepted,
}


# ── Utility ───────────────────────────────────────────────────────────────────

def _primary_email(data: dict) -> str:
    primary_id = data.get("primary_email_address_id")
    for entry in data.get("email_addresses", []):
        if entry.get("id") == primary_id:
            return entry["email_address"]
    return ""
