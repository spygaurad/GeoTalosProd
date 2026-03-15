from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.common import ORMModel, PaginatedResponse


class DatasetCreate(ORMModel):
    model_config = ConfigDict(populate_by_name=True)

    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    dataset_type: str = Field(min_length=1, max_length=50)
    stac_collection_id: str | None = None
    geometry: dict | None = None
    temporal_extent: dict | None = None
    metadata: dict | None = Field(
        default=None,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    created_by: UUID | None = None


class DatasetUpdate(ORMModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    dataset_type: str | None = Field(default=None, min_length=1, max_length=50)
    stac_collection_id: str | None = None
    geometry: dict | None = None
    temporal_extent: dict | None = None
    metadata: dict | None = Field(
        default=None,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    deleted_at: datetime | None = None


class DatasetRead(ORMModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    dataset_type: str
    stac_collection_id: str | None
    geometry: dict | None
    temporal_extent: dict | None
    metadata: dict | None = Field(
        default=None,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class DatasetListResponse(PaginatedResponse):
    items: list[DatasetRead]
