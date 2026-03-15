from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_role, get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.organization_member import (
    OrganizationMemberCreate,
    OrganizationMemberListResponse,
    OrganizationMemberRead,
    OrganizationMemberUpdate,
)
from app.services.membership_service import MembershipService

router = APIRouter(prefix="/organization-members", tags=["organization-members"])


@router.get("", response_model=OrganizationMemberListResponse)
async def list_organization_members(
    organization_id: UUID | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_current_role),
):
    if organization_id is not None and organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if role != "org:admin":
        if user_id is not None and user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        user_id = current_user.id
    service = MembershipService(db)
    items, total = await service.list_org_memberships(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        user_id=user_id,
    )
    return OrganizationMemberListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{organization_id}/{user_id}", response_model=OrganizationMemberRead)
async def get_organization_member_by_id(
    organization_id: UUID,
    user_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_current_role),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if role != "org:admin" and user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MembershipService(db)
    return await service.get_org_membership(organization_id, user_id)


@router.post("", response_model=OrganizationMemberRead, status_code=status.HTTP_201_CREATED)
async def create_organization_member(
    payload: OrganizationMemberCreate,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MembershipService(db)
    membership = await service.create_org_membership(payload)
    await log_audit_event(
        action="organization_members.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="organization_member",
        entity_id=f"{membership.organization_id}:{membership.user_id}",
        session=db,
    )
    return membership


@router.patch("/{organization_id}/{user_id}", response_model=OrganizationMemberRead)
async def update_organization_member_by_id(
    organization_id: UUID,
    user_id: UUID,
    payload: OrganizationMemberUpdate,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MembershipService(db)
    membership = await service.update_org_membership(organization_id, user_id, payload)
    await log_audit_event(
        action="organization_members.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="organization_member",
        entity_id=f"{organization_id}:{user_id}",
        session=db,
    )
    return membership


@router.delete("/{organization_id}/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization_member_by_id(
    organization_id: UUID,
    user_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MembershipService(db)
    await service.delete_org_membership(organization_id, user_id)
    await log_audit_event(
        action="organization_members.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="organization_member",
        entity_id=f"{organization_id}:{user_id}",
        session=db,
    )
