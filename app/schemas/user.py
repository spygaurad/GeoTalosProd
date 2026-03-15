from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class UserCreate(ORMModel):
    clerk_id: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=500)


class UserUpdate(ORMModel):
    email: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=500)


class UserRead(ORMModel):
    id: UUID
    clerk_id: str
    email: str
    name: str | None
    avatar_url: str | None
    created_at: datetime
    updated_at: datetime


class UserListResponse(PaginatedResponse):
    items: list[UserRead]
