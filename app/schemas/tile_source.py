from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class TileSourceCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    source_type: str = Field(pattern=r"^(raster|vector|basemap)$")
    tile_service_url: str | None = Field(default=None, max_length=500)
    config: dict | None = None
    dataset_item_id: UUID | None = None
    basemap_id: UUID | None = None


class TileSourceUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    tile_service_url: str | None = Field(default=None, max_length=500)
    config: dict | None = None


class TileSourceRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    source_type: str
    tile_service_url: str | None
    config: dict | None
    dataset_item_id: UUID | None
    basemap_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


TileSourceListResponse = PaginatedResponse[TileSourceRead]
