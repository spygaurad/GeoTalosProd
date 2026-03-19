from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class StyleCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=50)
    definition: dict


class StyleUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: str | None = Field(default=None, min_length=1, max_length=50)
    definition: dict | None = None


class StyleRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    type: str
    definition: dict
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


StyleListResponse = PaginatedResponse[StyleRead]
