from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class ProjectCreate(ORMModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    created_by: UUID | None = None


class ProjectRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


ProjectListResponse = PaginatedResponse[ProjectRead]
