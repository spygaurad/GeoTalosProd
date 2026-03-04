from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class ProjectCreate(ORMModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = None
    created_by: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    status: str = Field(default="active", min_length=1, max_length=30)


class ProjectUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    metadata: dict | None = None
    status: str | None = Field(default=None, min_length=1, max_length=30)
    archived_by: UUID | None = None
    archived_at: datetime | None = None


class ProjectRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    slug: str
    description: str | None
    created_by: UUID | None
    metadata: dict = Field(validation_alias="metadata_")
    status: str
    archived_at: datetime | None
    archived_by: UUID | None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(PaginatedResponse):
    items: list[ProjectRead]
