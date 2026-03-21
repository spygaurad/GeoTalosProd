from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.tile_source import TileSourceCreate, TileSourceListResponse, TileSourceRead, TileSourceUpdate
from app.services.tile_source_service import TileSourceService

router = APIRouter(prefix="/tile-sources", tags=["tile-sources"])


@router.get("", response_model=TileSourceListResponse)
async def list_tile_sources(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = TileSourceService(db)
    items, total = await service.list_tile_sources(limit=limit, offset=offset, organization_id=org_id)
    return TileSourceListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{tile_source_id}", response_model=TileSourceRead)
async def get_tile_source(
    tile_source_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = TileSourceService(db)
    return await service.get_tile_source(tile_source_id, organization_id=org_id)


@router.post("", response_model=TileSourceRead, status_code=status.HTTP_201_CREATED)
async def create_tile_source(
    payload: TileSourceCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = TileSourceService(db)
    ts = await service.create_tile_source(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="tile_sources.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="tile_source",
        entity_id=str(ts.id),
        session=db,
    )
    return ts


@router.patch("/{tile_source_id}", response_model=TileSourceRead)
async def update_tile_source(
    tile_source_id: UUID,
    payload: TileSourceUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = TileSourceService(db)
    ts = await service.update_tile_source(tile_source_id, payload, organization_id=org_id)
    await log_audit_event(
        action="tile_sources.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="tile_source",
        entity_id=str(tile_source_id),
        session=db,
    )
    return ts


@router.delete("/{tile_source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tile_source(
    tile_source_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = TileSourceService(db)
    await service.delete_tile_source(tile_source_id, organization_id=org_id)
    await log_audit_event(
        action="tile_sources.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="tile_source",
        entity_id=str(tile_source_id),
        session=db,
    )
