from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role, require_role
from app.core.deps import limit_param, offset_param
from app.core.audit import log_audit_event
from app.models.user import User
from app.schemas.organization import (
    OrganizationCreate,
    OrganizationListResponse,
    OrganizationRead,
    OrganizationUpdate,
)
from app.services.organization_service import OrganizationService

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=OrganizationListResponse)
async def list_organizations(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    organization_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = OrganizationService(db)
    items, total = await service.list_organizations(
        limit=limit,
        offset=offset,
        organization_id=organization_id,
    )
    return OrganizationListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{organization_id}", response_model=OrganizationRead)
async def get_organization_by_id(
    organization_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = OrganizationService(db)
    return await service.get_organization(organization_id)


@router.post("", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    db: AsyncSession = Depends(get_session),
    _role_guard: None = Depends(require_role("org:admin")),
    current_user: User = Depends(get_current_user),
):
    service = OrganizationService(db)
    organization = await service.create_organization(payload)
    await log_audit_event(
        action="organizations.create",
        actor_id=str(current_user.id),
        organization_id=str(organization.id),
        entity="organization",
        entity_id=str(organization.id),
        session=db,
    )
    return organization


@router.patch("/{organization_id}", response_model=OrganizationRead)
async def update_organization_by_id(
    organization_id: UUID,
    payload: OrganizationUpdate,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = OrganizationService(db)
    organization = await service.update_organization(organization_id, payload)
    await log_audit_event(
        action="organizations.update",
        actor_id=str(current_user.id),
        organization_id=str(organization_id),
        entity="organization",
        entity_id=str(organization_id),
        session=db,
    )
    return organization


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization_by_id(
    organization_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = OrganizationService(db)
    await service.delete_organization(organization_id)
    await log_audit_event(
        action="organizations.delete",
        actor_id=str(current_user.id),
        organization_id=str(organization_id),
        entity="organization",
        entity_id=str(organization_id),
        session=db,
    )
