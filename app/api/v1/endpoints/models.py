from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.ai_model import AIModelCreate, AIModelListResponse, AIModelRead, AIModelUpdate
from app.services.model_service import AIModelService

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=AIModelListResponse)
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
    service = AIModelService(db)
    items, total = await service.list_models(
        limit=limit,
        offset=offset,
        organization_id=org_id,
    )
    return AIModelListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{model_id}", response_model=AIModelRead)
async def get_model_by_id(
    model_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AIModelService(db)
    return await service.get_model(model_id, organization_id=org_id)


@router.post("", response_model=AIModelRead, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: AIModelCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AIModelService(db)
    model = await service.create_model(payload, organization_id=org_id)
    await log_audit_event(
        action="models.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model.id),
        session=db,
    )
    return model


@router.patch("/{model_id}", response_model=AIModelRead)
async def update_model_by_id(
    model_id: UUID,
    payload: AIModelUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AIModelService(db)
    model = await service.update_model(model_id, payload, organization_id=org_id)
    await log_audit_event(
        action="models.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model_id),
        session=db,
    )
    return model


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_by_id(
    model_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AIModelService(db)
    await service.delete_model(model_id, organization_id=org_id)
    await log_audit_event(
        action="models.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="model",
        entity_id=str(model_id),
        session=db,
    )
