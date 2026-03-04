from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import limit_param, offset_param
from app.db.session import get_db
from app.schemas.user import UserCreate, UserListResponse, UserRead, UserUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserListResponse)
async def list_users(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    db: AsyncSession = Depends(get_db),
):
    service = UserService(db)
    items, total = await service.list_users(limit=limit, offset=offset)
    return UserListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{user_id}", response_model=UserRead)
async def get_user_by_id(user_id: UUID, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    return await service.get_user(user_id)


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    return await service.create_user(payload)


@router.patch("/{user_id}", response_model=UserRead)
async def update_user_by_id(
    user_id: UUID,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = UserService(db)
    return await service.update_user(user_id, payload)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_by_id(user_id: UUID, db: AsyncSession = Depends(get_db)):
    service = UserService(db)
    await service.delete_user(user_id)
