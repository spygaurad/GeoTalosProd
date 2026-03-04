from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import limit_param, offset_param
from app.db.session import get_db
from app.schemas.org_membership import (
    OrgMembershipCreate,
    OrgMembershipListResponse,
    OrgMembershipRead,
    OrgMembershipUpdate,
)
from app.services.membership_service import MembershipService

router = APIRouter(prefix="/org-memberships", tags=["org-memberships"])


@router.get("", response_model=OrgMembershipListResponse)
async def list_org_memberships(
    organization_id: UUID | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    db: AsyncSession = Depends(get_db),
):
    service = MembershipService(db)
    items, total = await service.list_org_memberships(
        limit=limit,
        offset=offset,
        organization_id=organization_id,
        user_id=user_id,
    )
    return OrgMembershipListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{organization_id}/{user_id}", response_model=OrgMembershipRead)
async def get_org_membership_by_id(
    organization_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    service = MembershipService(db)
    return await service.get_org_membership(organization_id, user_id)


@router.post("", response_model=OrgMembershipRead, status_code=status.HTTP_201_CREATED)
async def create_org_membership(
    payload: OrgMembershipCreate, db: AsyncSession = Depends(get_db)
):
    service = MembershipService(db)
    return await service.create_org_membership(payload)


@router.patch("/{organization_id}/{user_id}", response_model=OrgMembershipRead)
async def update_org_membership_by_id(
    organization_id: UUID,
    user_id: UUID,
    payload: OrgMembershipUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = MembershipService(db)
    return await service.update_org_membership(organization_id, user_id, payload)


@router.delete("/{organization_id}/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org_membership_by_id(
    organization_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    service = MembershipService(db)
    await service.delete_org_membership(organization_id, user_id)
