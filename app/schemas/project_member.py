from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class ProjectMemberCreate(ORMModel):
    project_id: UUID
    user_id: UUID
    role: str = Field(default="viewer", min_length=1, max_length=50)
    added_by: UUID | None = None
    status: str = Field(default="active", min_length=1, max_length=20)


class ProjectMemberUpdate(ORMModel):
    role: str | None = Field(default=None, min_length=1, max_length=50)
    added_by: UUID | None = None
    status: str | None = Field(default=None, min_length=1, max_length=20)


class ProjectMemberRead(ORMModel):
    project_id: UUID
    user_id: UUID
    role: str
    added_by: UUID | None
    status: str
    created_at: datetime
    updated_at: datetime


class ProjectMemberListResponse(PaginatedResponse):
    items: list[ProjectMemberRead]
