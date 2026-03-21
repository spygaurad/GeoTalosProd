from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class BasemapCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    file_type: str = Field(pattern=r"^(pmtiles|mbtiles)$")
    local_path: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    config: dict | None = None


class BasemapUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    local_path: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=500)
    config: dict | None = None


class BasemapRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    file_type: str
    local_path: str | None
    url: str | None
    config: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


BasemapListResponse = PaginatedResponse[BasemapRead]
