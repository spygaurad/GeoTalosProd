from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSetCreate(ORMModel):
    # Required since migration 047 — every set carries class semantics, either
    # a real schema or a legacy placeholder created by the backfill.
    schema_id: UUID
    dataset_id: UUID | None = None
    dataset_item_id: UUID | None = None
    source_type: str = Field(default="manual", pattern=r"^(manual|model|import|analysis)$")
    model_id: UUID | None = None
    job_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetLinkRequest(ORMModel):
    annotation_set_id: UUID


class AnnotationSetMountRequest(ORMModel):
    annotation_set_id: UUID
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z_index: int = 0
    style_id: UUID | None = None
    style_override: dict | None = None


class AnnotationSetMountUpdate(ORMModel):
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    z_index: int | None = None
    style_id: UUID | None = None
    style_override: dict | None = None


class AnnotationSetMountRead(ORMModel):
    map_id: UUID
    annotation_set_id: UUID
    visible: bool
    opacity: float
    z_index: int
    style_id: UUID | None
    style_override: dict | None
    mounted_at: datetime


class AnnotationSetMountListResponse(ORMModel):
    items: list[AnnotationSetMountRead]
    total: int


class AnnotationSetProjectLinkRead(ORMModel):
    project_id: UUID
    annotation_set_id: UUID
    linked_at: datetime
    linked_by: UUID | None


class AnnotationSetProjectLinkListResponse(ORMModel):
    items: list[AnnotationSetProjectLinkRead]
    total: int


class AnnotationSetUpdate(ORMModel):
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    dataset_item_id: UUID | None = None
    source_type: str | None = Field(default=None, pattern=r"^(manual|model|import|analysis)$")
    model_id: UUID | None = None
    job_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetRead(ORMModel):
    id: UUID
    organization_id: UUID
    schema_id: UUID
    dataset_id: UUID | None
    dataset_item_id: UUID | None
    source_type: str
    model_id: UUID | None
    job_id: UUID | None
    name: str
    description: str | None
    raster_config: dict | None = None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


AnnotationSetListResponse = PaginatedResponse[AnnotationSetRead]


# ── Raster mask configuration (annotation sets sourced from segmentation masks) ──

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
