from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class OrgMembershipCreate(ORMModel):
    user_id: UUID
    organization_id: UUID
    role: str = Field(default="org:viewer", min_length=1, max_length=50)
    invited_by: UUID | None = None
    status: str = Field(default="active", min_length=1, max_length=20)


class OrgMembershipUpdate(ORMModel):
    role: str | None = Field(default=None, min_length=1, max_length=50)
    invited_by: UUID | None = None
    status: str | None = Field(default=None, min_length=1, max_length=20)


class OrgMembershipRead(ORMModel):
    user_id: UUID
    organization_id: UUID
    role: str
    invited_by: UUID | None
    status: str
    created_at: datetime
    synced_at: datetime
    updated_at: datetime


class OrgMembershipListResponse(PaginatedResponse):
    items: list[OrgMembershipRead]
