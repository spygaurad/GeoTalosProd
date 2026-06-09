from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import DatasetType
from app.core.geometry import serialize_geometry
from app.core.ranges import tstzrange_to_dict
from app.schemas.common import ORMModel, PaginatedResponse

_ALLOWED_DATASET_TYPES = {dt.value for dt in DatasetType}


class _DatasetBase(ORMModel):
    """Shared helper — provides ``model_dump_db`` to rename ``metadata`` → ``metadata_``."""

    model_config = ConfigDict(populate_by_name=True)

    def model_dump_db(self, **kwargs: Any) -> dict:
        data = self.model_dump(**kwargs)
        if "metadata" in data:
            data["metadata_"] = data.pop("metadata")
        return data


class DatasetCreate(_DatasetBase):
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

    @field_validator("dataset_type")
    @classmethod
    def _validate_dataset_type(cls, v: str) -> str:
        if v not in _ALLOWED_DATASET_TYPES:
            raise ValueError(
                f"dataset_type must be one of {sorted(_ALLOWED_DATASET_TYPES)}"
            )
        return v


class DatasetUpdate(_DatasetBase):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    dataset_type: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("dataset_type")
    @classmethod
    def _validate_dataset_type(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWED_DATASET_TYPES:
            raise ValueError(
                f"dataset_type must be one of {sorted(_ALLOWED_DATASET_TYPES)}"
            )
        return v
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
    status: str
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

    @field_validator("geometry", mode="before")
    @classmethod
    def _coerce_geometry(cls, value: Any) -> dict | None:
        return serialize_geometry(value)

    @field_validator("temporal_extent", mode="before")
    @classmethod
    def _coerce_temporal_extent(cls, value: Any) -> dict | None:
        return tstzrange_to_dict(value)


DatasetListResponse = PaginatedResponse[DatasetRead]


# ── Segmentation-mask class mapping schemas ───────────────────────────────────

class DatasetRasterValuesRead(ORMModel):
    """Unique pixel values read live from a segmentation-mask dataset's band,
    for building the value→class mapping UI."""

    dataset_id: UUID
    dataset_item_id: UUID
    band_index: int
    values: list[float]
    total_unique: int
    truncated: bool


class DatasetClassMapUpdate(ORMModel):
    """Map raster pixel values to annotation classes so the mask overlay renders
    with class colors. Colors themselves are derived client-side from the
    classes' styles — only the value→class association is stored here."""

    schema_id: UUID
    # {pixel_value (as string) → annotation class UUID}
    value_class_map: dict[str, UUID]
    band_index: int = Field(default=1, ge=1)
    nodata_value: float | None = None


class DatasetClassMapRead(ORMModel):
    dataset_id: UUID
    schema_id: UUID
    band_index: int
    nodata_value: float | None
    value_class_map: dict[str, UUID]


# ── Upload sub-resource schemas ───────────────────────────────────────────────

_ALLOWED_CONTENT_TYPES = {"image/tiff", "application/zip", "application/x-zip-compressed"}


class UploadInitiateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    file_size_bytes: int = Field(gt=0)
    content_type: str = "image/tiff"

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, v: str) -> str:
        if v not in _ALLOWED_CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {sorted(_ALLOWED_CONTENT_TYPES)}")
        return v


class UploadPartUrl(BaseModel):
    part_number: int
    url: str


class UploadInitiateResponse(BaseModel):
    upload_id: str
    job_id: UUID
    s3_key: str
    part_size_bytes: int
    total_parts: int
    part_urls: list[UploadPartUrl]


class PartUrlsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1)


class PartUrlsResponse(BaseModel):
    part_urls: list[UploadPartUrl]


class UploadPart(BaseModel):
    part_number: int
    etag: str


class UploadCompleteRequest(BaseModel):
    # parts is optional: MinIO Community cannot expose ETag via CORS, so
    # clients may not be able to collect them.  When omitted, the API lists
    # uploaded parts from MinIO server-side before calling CompleteMultipartUpload.
    parts: list[UploadPart] | None = None


class UploadJobResponse(BaseModel):
    job_id: UUID
