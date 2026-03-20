from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.style import StyleCreate, StyleListResponse, StyleRead, StyleUpdate
from app.services.style_service import StyleService

router = APIRouter(prefix="/styles", tags=["styles"])


@router.get("", response_model=StyleListResponse)
async def list_styles(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = StyleService(db)
    items, total = await service.list_styles(limit=limit, offset=offset, organization_id=org_id)
    return StyleListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{style_id}", response_model=StyleRead)
async def get_style(
    style_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = StyleService(db)
    return await service.get_style(style_id, organization_id=org_id)


@router.post("", response_model=StyleRead, status_code=status.HTTP_201_CREATED)
async def create_style(
    payload: StyleCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = StyleService(db)
    style = await service.create_style(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="styles.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="style",
        entity_id=str(style.id),
        session=db,
    )
    return style


@router.patch("/{style_id}", response_model=StyleRead)
async def update_style(
    style_id: UUID,
    payload: StyleUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = StyleService(db)
    style = await service.update_style(style_id, payload, organization_id=org_id)
    await log_audit_event(
        action="styles.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="style",
        entity_id=str(style_id),
        session=db,
    )
    return style


@router.delete("/{style_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_style(
    style_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = StyleService(db)
    await service.delete_style(style_id, organization_id=org_id)
    await log_audit_event(
        action="styles.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="style",
        entity_id=str(style_id),
        session=db,
    )
