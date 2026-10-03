from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import ORMModel


class JobRead(ORMModel):
    id: UUID
    organization_id: UUID
    type: str
    status: str
    config: dict | None
    input_refs: list | None
    processed_items: int
    total_items: int
    failed_items: int
    progress: float | None
    logs: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConvertToCogJobCreate(ORMModel):
    """Payload for ``POST /jobs/convert-to-cog``."""

    dataset_id: UUID
    dataset_item_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        description="If omitted, every active item on the dataset is converted.",
    )
    dataset_name: str | None = Field(default=None, max_length=255)
    project_id: UUID | None = None


class InferenceJobCreate(ORMModel):
    """Payload for ``POST /jobs/inference`` — model-agnostic batch inference."""

    model_id: UUID
    dataset_item_ids: list[UUID] = Field(min_length=1)
    project_id: UUID | None = None
    map_id: UUID | None = None
    mount_on_map: bool = False
    aoi_bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    prompt_payload: dict[str, Any] | None = None
    output_class_id: UUID | None = Field(
        default=None,
        description=(
            "When set, every prediction returned by the model is assigned to this "
            "annotation_class_id, bypassing label-based mapping. Use for prompted "
            "models (e.g. SAM3 text) where the user picks the class explicitly and "
            "the prompt is just a hint to the endpoint."
        ),
    )
    render_params: dict[str, str] | None = Field(
        default=None,
        description=(
            "TiTiler query params used when rendering per-patch PNGs sent to the "
            "model endpoint. Typically {asset_bidx, rescale} computed from the AOI "
            "source layer's band selection. Overrides the dataset-level default "
            "preset; falls back to it when omitted."
        ),
    )
    patch_size_px: int | None = Field(default=None, ge=64, le=4096)
    stride_px: int | None = Field(default=None, ge=32, le=4096)
    max_patches_per_item: int | None = Field(default=None, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_aoi_bbox(self):
        if self.aoi_bbox is None:
            return self
        minx, miny, maxx, maxy = self.aoi_bbox
        if minx >= maxx or miny >= maxy:
            raise ValueError("aoi_bbox must be [minx, miny, maxx, maxy] with min < max")
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise ValueError("aoi_bbox must be within EPSG:4326 bounds")
        return self


class ExtractRasterFeaturesJobCreate(ORMModel):
    """Payload for ``POST /jobs/extract-raster-features``."""

    dataset_item_id: UUID
    schema_id: UUID
    output_class_id: UUID
    band_index: int = Field(default=1, ge=1)
    threshold_min: float
    threshold_max: float | None = None
    min_area_px: float = Field(default=0.0, ge=0)
    simplify_tolerance: float | None = None
    connectivity: Literal[4, 8] = 4
    dissolve: bool = False
    annotation_set_name: str | None = None
    index: Literal["band"] = "band"

    @model_validator(mode="after")
    def validate_threshold_range(self):
        if self.threshold_max is not None and self.threshold_max < self.threshold_min:
            raise ValueError("threshold_max must be greater than or equal to threshold_min")
        return self
