from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_role, get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.user import UserCreate, UserListResponse, UserRead, UserUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserListResponse)
async def list_users(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    organization_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_current_role),
):
    user_filter = None if role == "org:admin" else current_user.id
    service = UserService(db)
    items, total = await service.list_users(
        limit=limit,
        offset=offset,
        organization_id=organization_id,
        user_id=user_filter,
    )
    return UserListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{user_id}", response_model=UserRead)
async def get_user_by_id(
    user_id: UUID,
    organization_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = UserService(db)
    return await service.get_user(user_id, organization_id=organization_id)


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    organization_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = UserService(db)
    user = await service.create_user(payload)
    log_audit_event(
        action="users.create",
        actor_id=str(current_user.id),
        organization_id=str(organization_id),
        entity="user",
        entity_id=str(user.id),
    )
    return user


@router.patch("/{user_id}", response_model=UserRead)
async def update_user_by_id(
    user_id: UUID,
    payload: UserUpdate,
    organization_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = UserService(db)
    user = await service.update_user(user_id, payload, organization_id=organization_id)
    log_audit_event(
        action="users.update",
        actor_id=str(current_user.id),
        organization_id=str(organization_id),
        entity="user",
        entity_id=str(user_id),
    )
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_by_id(
    user_id: UUID,
    organization_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = UserService(db)
    await service.delete_user(user_id, organization_id=organization_id)
    log_audit_event(
        action="users.delete",
        actor_id=str(current_user.id),
        organization_id=str(organization_id),
        entity="user",
        entity_id=str(user_id),
    )
