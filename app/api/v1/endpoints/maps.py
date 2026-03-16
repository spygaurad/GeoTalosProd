from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.map import MapCreate, MapListResponse, MapRead, MapUpdate
from app.services.map_service import MapService
from app.services.project_service import ProjectService

router = APIRouter(prefix="/maps", tags=["maps"])


@router.get("", response_model=MapListResponse)
async def list_maps(
    project_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    items, total = await service.list_maps(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        project_id=project_id,
    )
    return MapListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{map_id}", response_model=MapRead)
async def get_map_by_id(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    return await service.get_map(map_id, organization_id=org_id)


@router.post("", response_model=MapRead, status_code=status.HTTP_201_CREATED)
async def create_map(
    payload: MapCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    project_service = ProjectService(db)
    project = await project_service.get_project(payload.project_id, organization_id=org_id)
    if project.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MapService(db)
    map_row = await service.create_map(payload, created_by=current_user.id)
    await log_audit_event(
        action="maps.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_row.id),
        session=db,
    )
    return map_row


@router.patch("/{map_id}", response_model=MapRead)
async def update_map_by_id(
    map_id: UUID,
    payload: MapUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    map_row = await service.update_map(map_id, payload, organization_id=org_id)
    await log_audit_event(
        action="maps.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_id),
        session=db,
    )
    return map_row


@router.delete("/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_map_by_id(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    await service.delete_map(map_id, organization_id=org_id)
    await log_audit_event(
        action="maps.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_id),
        session=db,
    )
