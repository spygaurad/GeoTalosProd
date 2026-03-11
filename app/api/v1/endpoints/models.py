from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.model import ModelCreate, ModelListResponse, ModelRead, ModelUpdate
from app.services.model_service import ModelService

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelListResponse)
async def list_models(
    organization_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    if organization_id is not None and organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = ModelService(db)
    items, total = await service.list_models(
        limit=limit,
        offset=offset,
        organization_id=org_id,
    )
    return ModelListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{model_id}", response_model=ModelRead)
async def get_model_by_id(
    model_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = ModelService(db)
    return await service.get_model(model_id, organization_id=org_id)


@router.post("", response_model=ModelRead, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = ModelService(db)
    model = await service.create_model(payload)
    log_audit_event(
        action="models.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model.id),
    )
    return model


@router.patch("/{model_id}", response_model=ModelRead)
async def update_model_by_id(
    model_id: UUID,
    payload: ModelUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ModelService(db)
    model = await service.update_model(model_id, payload, organization_id=org_id)
    log_audit_event(
        action="models.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model_id),
    )
    return model


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_by_id(
    model_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ModelService(db)
    await service.delete_model(model_id, organization_id=org_id)
    log_audit_event(
        action="models.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model_id),
    )
