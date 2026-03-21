from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.basemap import BasemapCreate, BasemapListResponse, BasemapRead, BasemapUpdate
from app.services.basemap_service import BasemapService

router = APIRouter(prefix="/basemaps", tags=["basemaps"])


@router.get("", response_model=BasemapListResponse)
async def list_basemaps(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = BasemapService(db)
    items, total = await service.list_basemaps(limit=limit, offset=offset, organization_id=org_id)
    return BasemapListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{basemap_id}", response_model=BasemapRead)
async def get_basemap(
    basemap_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = BasemapService(db)
    return await service.get_basemap(basemap_id, organization_id=org_id)


@router.post("", response_model=BasemapRead, status_code=status.HTTP_201_CREATED)
async def create_basemap(
    payload: BasemapCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = BasemapService(db)
    basemap = await service.create_basemap(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="basemaps.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="basemap",
        entity_id=str(basemap.id),
        session=db,
    )
    return basemap


@router.patch("/{basemap_id}", response_model=BasemapRead)
async def update_basemap(
    basemap_id: UUID,
    payload: BasemapUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = BasemapService(db)
    basemap = await service.update_basemap(basemap_id, payload, organization_id=org_id)
    await log_audit_event(
        action="basemaps.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="basemap",
        entity_id=str(basemap_id),
        session=db,
    )
    return basemap


@router.delete("/{basemap_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_basemap(
    basemap_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = BasemapService(db)
    await service.delete_basemap(basemap_id, organization_id=org_id)
    await log_audit_event(
        action="basemaps.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="basemap",
        entity_id=str(basemap_id),
        session=db,
    )
