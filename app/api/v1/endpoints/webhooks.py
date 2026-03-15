"""Clerk webhook receiver.

Secured by X-Internal-Key header (not a Clerk JWT). This endpoint is NOT on
the public internet — the Next.js frontend verifies the Svix signature and
relays the event here over the internal network.

Handled events:
  user.created / user.updated / user.deleted
  organization.created / organization.updated / organization.deleted
  organizationMembership.created / organizationMembership.updated / organizationMembership.deleted

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
from app.models.organization import Organization
from app.models.organization_member import OrganizationMember
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
    clerk_id = data["id"]
    email = _primary_email(data)
    name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(User.__table__)
                .values(id=uuid.uuid4(), clerk_id=clerk_id, email=email, name=name)
                .on_conflict_do_update(
                    index_elements=["clerk_id"],
                    set_={"email": email, "name": name},
                )
            )


async def _handle_user_updated(data: dict) -> None:
    clerk_id = data["id"]
    email = _primary_email(data)
    name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(User.__table__)
                .values(id=uuid.uuid4(), clerk_id=clerk_id, email=email, name=name)
                .on_conflict_do_update(
                    index_elements=["clerk_id"],
                    set_={"email": email, "name": name},
                )
            )


async def _handle_user_deleted(data: dict) -> None:
    # Keep user row for auditing/history; no soft delete in v3 schema yet.
    return None


async def _handle_org_created(data: dict) -> None:
    clerk_org_id = data["id"]
    name = data["name"]
    slug = data.get("slug") or _slugify(name)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(Organization.__table__)
                .values(id=uuid.uuid4(), clerk_org_id=clerk_org_id, name=name, slug=slug)
                .on_conflict_do_update(
                    index_elements=["clerk_org_id"],
                    set_={"name": name},
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
    # Orgs with data cannot be safely hard-deleted; leave record intact.
    return None


def _map_clerk_role(role: str) -> str:
    return {"org:admin": "admin", "org:member": "member"}.get(role, "viewer")


async def _handle_membership_created(data: dict) -> None:
    clerk_id = data["public_user_data"]["user_id"]
    clerk_org_id = data["organization"]["id"]
    clerk_role = data.get("role", "org:viewer")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            user = (
                await session.execute(select(User).where(User.clerk_id == clerk_id))
            ).scalar_one_or_none()

            org = (
                await session.execute(
                    select(Organization).where(Organization.clerk_org_id == clerk_org_id)
                )
            ).scalar_one_or_none()

            if not user or not org:
                return

            await session.execute(
                pg_insert(OrganizationMember.__table__)
                .values(
                    organization_id=org.id,
                    user_id=user.id,
                    role=_map_clerk_role(clerk_role),
                )
                .on_conflict_do_update(
                    index_elements=["organization_id", "user_id"],
                    set_={"role": _map_clerk_role(clerk_role)},
                )
            )


async def _handle_membership_updated(data: dict) -> None:
    await _handle_membership_created(data)


async def _handle_membership_deleted(data: dict) -> None:
    clerk_id = data["public_user_data"]["user_id"]
    clerk_org_id = data["organization"]["id"]

    async with AsyncSessionLocal() as session:
        async with session.begin():
            user = (
                await session.execute(select(User).where(User.clerk_id == clerk_id))
            ).scalar_one_or_none()

            org = (
                await session.execute(
                    select(Organization).where(Organization.clerk_org_id == clerk_org_id)
                )
            ).scalar_one_or_none()

            if not user or not org:
                return

            await session.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.organization_id == org.id,
                    OrganizationMember.user_id == user.id,
                )
            )


def _primary_email(data: dict) -> str:
    emails = data.get("email_addresses", [])
    if not emails:
        return ""
    if data.get("primary_email_address_id"):
        for entry in emails:
            if entry.get("id") == data.get("primary_email_address_id"):
                return entry.get("email_address", "")
    return emails[0].get("email_address", "")


_HANDLERS: dict[str, Any] = {
    "user.created": _handle_user_created,
    "user.updated": _handle_user_updated,
    "user.deleted": _handle_user_deleted,
    "organization.created": _handle_org_created,
    "organization.updated": _handle_org_updated,
    "organization.deleted": _handle_org_deleted,
    "organizationMembership.created": _handle_membership_created,
    "organizationMembership.updated": _handle_membership_updated,
    "organizationMembership.deleted": _handle_membership_deleted,
}
