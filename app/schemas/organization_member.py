from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class OrganizationMemberCreate(ORMModel):
    organization_id: UUID
    user_id: UUID
    role: str = Field(min_length=1, max_length=20)


class OrganizationMemberUpdate(ORMModel):
    role: str | None = Field(default=None, min_length=1, max_length=20)


class OrganizationMemberRead(ORMModel):
    organization_id: UUID
    user_id: UUID
    role: str
    joined_at: datetime


OrganizationMemberListResponse = PaginatedResponse[OrganizationMemberRead]
