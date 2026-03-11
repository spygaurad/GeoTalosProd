from datetime import datetime
from uuid import UUID

from pydantic import Field, field_serializer

from app.core.geometry import serialize_geometry
from app.schemas.common import ORMModel, PaginatedResponse


class DatasetCreate(ORMModel):
    organization_id: UUID
    project_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    stac_collection_id: str | None = None
    source_uri: str = Field(min_length=1)
    file_format: str | None = None
    status: str = Field(default="pending", min_length=1, max_length=50)
    tags: list[str] = Field(default_factory=list)
    temporal_extent_start: datetime | None = None
    temporal_extent_end: datetime | None = None
    spatial_extent: dict | None = None
    license: str | None = None
    item_count: int = 0
    total_size_bytes: int = 0
    parent_dataset_id: UUID | None = None
    created_by: UUID | None = None
    metadata: dict = Field(default_factory=dict)


class DatasetUpdate(ORMModel):
    project_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    stac_collection_id: str | None = None
    source_uri: str | None = None
    file_format: str | None = None
    status: str | None = Field(default=None, min_length=1, max_length=50)
    tags: list[str] | None = None
    temporal_extent_start: datetime | None = None
    temporal_extent_end: datetime | None = None
    spatial_extent: dict | None = None
    license: str | None = None
    item_count: int | None = None
    total_size_bytes: int | None = None
    parent_dataset_id: UUID | None = None
    metadata: dict | None = None


class DatasetRead(ORMModel):
    id: UUID
    organization_id: UUID
    project_id: UUID | None
    name: str
    description: str | None
    stac_collection_id: str | None
    source_uri: str
    file_format: str | None
    status: str
    tags: list[str] = Field(default_factory=list)
    temporal_extent_start: datetime | None
    temporal_extent_end: datetime | None
    spatial_extent: dict | None = None
    license: str | None
    item_count: int
    total_size_bytes: int
    parent_dataset_id: UUID | None
    created_by: UUID | None
    metadata: dict = Field(default_factory=dict, validation_alias="metadata_")
    created_at: datetime
    updated_at: datetime

    @field_serializer("spatial_extent")
    def _serialize_spatial_extent(self, value):
        return serialize_geometry(value)


class DatasetListResponse(PaginatedResponse):
    items: list[DatasetRead]
