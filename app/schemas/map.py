from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class MapCreate(ORMModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=255)
    view_state: dict
    base_style: dict | None = None
    created_by: UUID | None = None


class MapUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    view_state: dict | None = None
    base_style: dict | None = None
    deleted_at: datetime | None = None


class MapRead(ORMModel):
    id: UUID
    project_id: UUID
    name: str
    view_state: dict
    base_style: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MapListResponse(PaginatedResponse):
    items: list[MapRead]
