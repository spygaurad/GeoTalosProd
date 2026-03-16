from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.ranges import tstzrange_to_dict
from app.schemas.common import ORMModel, PaginatedResponse


class _DatasetBase(ORMModel):
    """Shared helper — provides ``model_dump_db`` to rename ``metadata`` → ``metadata_``."""

    model_config = ConfigDict(populate_by_name=True)

    def model_dump_db(self, **kwargs: Any) -> dict:
        data = self.model_dump(**kwargs)
        if "metadata" in data:
            data["metadata_"] = data.pop("metadata")
        return data


class DatasetCreate(_DatasetBase):
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


class DatasetUpdate(_DatasetBase):
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

    @field_validator("temporal_extent", mode="before")
    @classmethod
    def _coerce_temporal_extent(cls, value: Any) -> dict | None:
        return tstzrange_to_dict(value)


DatasetListResponse = PaginatedResponse[DatasetRead]


# ── Upload sub-resource schemas ───────────────────────────────────────────────

class UploadInitiateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    file_size_bytes: int = Field(gt=0)
    content_type: str = "image/tiff"


class UploadPartUrl(BaseModel):
    part_number: int
    url: str


class UploadInitiateResponse(BaseModel):
    upload_id: str
    job_id: UUID
    s3_key: str
    part_size_bytes: int
    part_urls: list[UploadPartUrl]


class PartUrlsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1)


class PartUrlsResponse(BaseModel):
    part_urls: list[UploadPartUrl]


class UploadPart(BaseModel):
    part_number: int
    etag: str


class UploadCompleteRequest(BaseModel):
    parts: list[UploadPart] = Field(min_length=1)


class UploadJobResponse(BaseModel):
    job_id: UUID
