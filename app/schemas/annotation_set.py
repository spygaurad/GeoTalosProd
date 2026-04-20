from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSetCreate(ORMModel):
    map_id: UUID | None = None
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    stac_item_id: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetUpdate(ORMModel):
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    stac_item_id: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetRead(ORMModel):
    id: UUID
    map_id: UUID | None
    schema_id: UUID | None
    dataset_id: UUID | None
    stac_item_id: str | None
    name: str
    description: str | None
    created_by_user_id: UUID | None
    created_by_job_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class RasterMaskConfigUpdate(ORMModel):
    dataset_item_id: str = Field(
        min_length=1,
        max_length=255,
        description="Dataset item UUID or STAC item ID",
    )
    map_layer_id: UUID | None = None
    band_index: int = Field(default=1, ge=1)
    nodata_value: float | None = 0
    value_class_map: dict[str, UUID] = Field(
        default_factory=dict,
        description="Mapping of raster pixel value -> annotation class UUID",
    )


class RasterMaskConfigRead(ORMModel):
    annotation_set_id: UUID
    map_layer_id: UUID | None
    dataset_item_id: UUID
    dataset_id: UUID
    stac_collection_id: str
    stac_item_id: str
    band_index: int
    nodata_value: float | None
    value_class_map: dict[str, UUID]
    colormap: dict[str, list[int]]
    tile_url_template: str


class RasterMaskValuesPreviewRead(ORMModel):
    dataset_item_id: UUID
    band_index: int
    values: list[float]
    total_unique: int
    truncated: bool


AnnotationSetListResponse = PaginatedResponse[AnnotationSetRead]
