from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.feature_layer import (
    FeatureLayerBulkCreate,
    FeatureLayerCreate,
    FeatureLayerListResponse,
    FeatureLayerRead,
)
from app.services.feature_layer_service import FeatureLayerService

router = APIRouter(prefix="/feature-layers", tags=["feature-layers"])


@router.get("", response_model=FeatureLayerListResponse)
async def list_feature_layers(
    layer_name: str | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    items, total = await service.list_features(
        limit=limit, offset=offset, organization_id=org_id, layer_name=layer_name
    )
    read_items = [FeatureLayerRead(**service.to_read_dict(f)) for f in items]
    return FeatureLayerListResponse(items=read_items, total=total, limit=limit, offset=offset)


@router.get("/{feature_id}", response_model=FeatureLayerRead)
async def get_feature_layer(
    feature_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    feature = await service.get_feature(feature_id, organization_id=org_id)
    return FeatureLayerRead(**service.to_read_dict(feature))


@router.post("", response_model=FeatureLayerRead, status_code=status.HTTP_201_CREATED)
async def create_feature_layer(
    payload: FeatureLayerCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    feature = await service.create_feature(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="feature_layers.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        entity_id=str(feature.id),
        session=db,
    )
    return FeatureLayerRead(**service.to_read_dict(feature))


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def bulk_create_feature_layers(
    payload: FeatureLayerBulkCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    created = await service.bulk_create_features(
        payload.features, organization_id=org_id, created_by=current_user.id
    )
    await log_audit_event(
        action="feature_layers.bulk_create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        extra={"count": created},
        session=db,
    )
    return {"created": created}


@router.delete("/{feature_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feature_layer(
    feature_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = FeatureLayerService(db)
    await service.delete_feature(feature_id, organization_id=org_id)
    await log_audit_event(
        action="feature_layers.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="feature_layer",
        entity_id=str(feature_id),
        session=db,
    )
