from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import ORMModel, PaginatedResponse
from app.schemas.dataset_item import DatasetItemRead
from app.schemas.job import InferenceJobCreate


class MapAOISelectionConfig(ORMModel):
    dataset_ids: list[UUID] = Field(default_factory=list)
    dataset_item_ids: list[UUID] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


class MapAOIRenderConfig(ORMModel):
    assets: str | None = None
    bands: list[int] | None = None
    asset_bidx: str | None = None
    rescale: str | None = None
    colormap: str | None = None
    rgb_mode: str | None = None
    extra: dict[str, Any] | None = None


class MapAOICreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    bbox_4326: list[float] = Field(min_length=4, max_length=4)
    geometry: dict | None = None
    selection_config: MapAOISelectionConfig | None = None
    render_config: MapAOIRenderConfig | None = None
    temporal_config: dict[str, Any] | None = None
    analysis_config: dict[str, Any] | None = None
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z_index: int = 0

    @model_validator(mode="after")
    def validate_bbox(self):
        minx, miny, maxx, maxy = self.bbox_4326
        if minx >= maxx or miny >= maxy:
            raise ValueError("bbox_4326 must be [minx, miny, maxx, maxy] with min < max")
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise ValueError("bbox_4326 must be within EPSG:4326 bounds")
        return self


class MapAOIUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    bbox_4326: list[float] | None = Field(default=None, min_length=4, max_length=4)
    geometry: dict | None = None
    selection_config: MapAOISelectionConfig | None = None
    render_config: MapAOIRenderConfig | None = None
    temporal_config: dict[str, Any] | None = None
    analysis_config: dict[str, Any] | None = None
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    z_index: int | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        if self.bbox_4326 is None:
            return self
        minx, miny, maxx, maxy = self.bbox_4326
        if minx >= maxx or miny >= maxy:
            raise ValueError("bbox_4326 must be [minx, miny, maxx, maxy] with min < max")
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise ValueError("bbox_4326 must be within EPSG:4326 bounds")
        return self


class MapAOIRead(ORMModel):
    id: UUID
    map_id: UUID
    organization_id: UUID
    name: str
    bbox_4326: list[float]
    geometry: dict | None
    selection_config: dict | None
    render_config: dict | None
    temporal_config: dict | None
    analysis_config: dict | None
    visible: bool
    opacity: float
    z_index: int
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


MapAOIListResponse = PaginatedResponse[MapAOIRead]


class MapAOITimelineRead(ORMModel):
    aoi_id: UUID
    bbox_4326: list[float]
    dataset_items: list[DatasetItemRead]


class MapAOITimelineManifestRead(ORMModel):
    aoi_id: UUID
    manifest_key: str
    frame_count: int
    bbox_4326: list[float]
    render_config: dict | None = None
    frames: list[dict]


class MapAOITileJSONRequest(ORMModel):
    assets: str | None = None
    preset: str | None = None
    rescale: str | None = None
    asset_bidx: str | None = None


class MapAOIInferenceCreate(ORMModel):
    model_id: UUID
    scope: str = Field(default="aoi", pattern="^(aoi|dataset)$")
    dataset_id: UUID | None = None
    dataset_item_ids: list[UUID] | None = None
    prompt_payload: dict[str, Any] | None = None
    output_class_id: UUID | None = None
    render_params: dict[str, str] | None = None
    mount_on_map: bool = True
    patch_size_px: int | None = Field(default=None, ge=64, le=4096)
    stride_px: int | None = Field(default=None, ge=32, le=4096)
    max_patches_per_item: int | None = Field(default=None, ge=1, le=4096)

    def to_inference_job(
        self,
        *,
        dataset_item_ids: list[UUID],
        map_id: UUID,
        project_id: UUID,
        aoi_bbox: list[float] | None,
    ) -> InferenceJobCreate:
        return InferenceJobCreate(
            model_id=self.model_id,
            dataset_item_ids=dataset_item_ids,
            project_id=project_id,
            map_id=map_id,
            mount_on_map=self.mount_on_map,
            aoi_bbox=aoi_bbox,
            prompt_payload=self.prompt_payload,
            output_class_id=self.output_class_id,
            render_params=self.render_params,
            patch_size_px=self.patch_size_px,
            stride_px=self.stride_px,
            max_patches_per_item=self.max_patches_per_item,
        )
