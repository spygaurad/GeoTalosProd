from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.map_layer import (
    MapLayerCreate,
    MapLayerListResponse,
    MapLayerRead,
    MapLayerReorderRequest,
    MapLayerUpdate,
)
from app.services.map_layer_service import MapLayerService

# Nested under /maps — path params include map_id from the parent router
router = APIRouter(prefix="/maps/{map_id}/layers", tags=["map-layers"])


@router.get("", response_model=MapLayerListResponse)
async def list_map_layers(
    map_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapLayerService(db)
    items, total = await service.list_layers(
        map_id=map_id,
        organization_id=org_id,
        limit=limit,
        offset=offset,
    )
    return MapLayerListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{layer_id}", response_model=MapLayerRead)
async def get_map_layer(
    map_id: UUID,
    layer_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapLayerService(db)
    return await service.get_layer(map_id=map_id, layer_id=layer_id, organization_id=org_id)


@router.post("", response_model=MapLayerRead, status_code=status.HTTP_201_CREATED)
async def create_map_layer(
    map_id: UUID,
    payload: MapLayerCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapLayerService(db)
    layer = await service.create_layer(
        map_id=map_id, organization_id=org_id, payload=payload
    )
    await log_audit_event(
        action="map_layers.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_layer",
        entity_id=str(layer.id),
        session=db,
    )
    return layer


@router.patch("/{layer_id}", response_model=MapLayerRead)
async def update_map_layer(
    map_id: UUID,
    layer_id: UUID,
    payload: MapLayerUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapLayerService(db)
    layer = await service.update_layer(
        map_id=map_id, layer_id=layer_id, organization_id=org_id, payload=payload
    )
    await log_audit_event(
        action="map_layers.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_layer",
        entity_id=str(layer_id),
        session=db,
    )
    return layer


@router.delete("/{layer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_map_layer(
    map_id: UUID,
    layer_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapLayerService(db)
    await service.delete_layer(map_id=map_id, layer_id=layer_id, organization_id=org_id)
    await log_audit_event(
        action="map_layers.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_layer",
        entity_id=str(layer_id),
        session=db,
    )


@router.put("/reorder", response_model=list[MapLayerRead])
async def reorder_map_layers(
    map_id: UUID,
    payload: MapLayerReorderRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """Reorder layers by providing an ordered list of layer IDs.

    The first ID becomes ``z_index = 0`` (rendered at the bottom of the stack),
    the last becomes ``z_index = N-1`` (rendered on top).  All provided IDs
    must belong to this map.  Layers not in the list keep their current z_index.
    """
    service = MapLayerService(db)
    layers = await service.reorder_layers(
        map_id=map_id, organization_id=org_id, layer_ids=payload.layer_ids
    )
    await log_audit_event(
        action="map_layers.reorder",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_id),
        session=db,
    )
    return layers
