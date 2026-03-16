from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class OrganizationCreate(ORMModel):
    clerk_org_id: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: str | None = None
    owner_id: UUID | None = None
    settings: dict = Field(default_factory=dict)


class OrganizationUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    owner_id: UUID | None = None
    settings: dict | None = None


class OrganizationRead(ORMModel):
    id: UUID
    clerk_org_id: str | None
    name: str
    slug: str
    description: str | None
    owner_id: UUID | None
    settings: dict
    created_at: datetime
    updated_at: datetime


OrganizationListResponse = PaginatedResponse[OrganizationRead]
