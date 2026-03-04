from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import limit_param, offset_param
from app.db.session import get_db
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
    db: AsyncSession = Depends(get_db),
):
    service = OrganizationService(db)
    items, total = await service.list_organizations(limit=limit, offset=offset)
    return OrganizationListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{organization_id}", response_model=OrganizationRead)
async def get_organization_by_id(organization_id: UUID, db: AsyncSession = Depends(get_db)):
    service = OrganizationService(db)
    return await service.get_organization(organization_id)


@router.post("", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate, db: AsyncSession = Depends(get_db)
):
    service = OrganizationService(db)
    return await service.create_organization(payload)


@router.patch("/{organization_id}", response_model=OrganizationRead)
async def update_organization_by_id(
    organization_id: UUID,
    payload: OrganizationUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = OrganizationService(db)
    return await service.update_organization(organization_id, payload)


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization_by_id(organization_id: UUID, db: AsyncSession = Depends(get_db)):
    service = OrganizationService(db)
    await service.delete_organization(organization_id)
