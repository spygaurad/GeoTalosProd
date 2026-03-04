from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class UserCreate(ORMModel):
    clerk_user_id: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class UserUpdate(ORMModel):
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class UserRead(ORMModel):
    id: UUID
    clerk_user_id: str
    email: str | None
    name: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserListResponse(PaginatedResponse):
    items: list[UserRead]
