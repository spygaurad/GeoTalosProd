from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse
from app.schemas.dataset import DatasetRead
from app.schemas.dataset_item import DatasetItemRead
from app.schemas.annotation_set import AnnotationSetRead
from app.schemas.job import InferenceJobCreate, JobRead
from app.schemas.map_layer import MapLayerRead


class MapCreate(ORMModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=255)
    view_state: dict
    base_style: dict | None = None


class LayerStateUpdate(ORMModel):
    """Partial layer state sent by the frontend during the 8-second debounced save.

    Only the fields the user actually changed need to be present.
    The ``id`` field identifies which layer to update.
    """

    id: UUID
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    style_override: dict | None = None


class MapUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    view_state: dict | None = None
    base_style: dict | None = None
    # Optional batch layer state — used by the unified 8-second debounced auto-save.
    # When present the service applies each LayerStateUpdate to its corresponding
    # MapLayer row in the same transaction as the map update.
    layers: list[LayerStateUpdate] | None = None


class MapRead(ORMModel):
    id: UUID
    project_id: UUID
    name: str
    view_state: dict
    base_style: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    # Ordered by z_index ascending — matches the visual stacking order on the map
    layers: list[MapLayerRead] = Field(default_factory=list)


MapListResponse = PaginatedResponse[MapRead]


class MapAOIResourcesRead(ORMModel):
    bbox: list[float]
    datasets: list[DatasetRead] = Field(default_factory=list)
    dataset_items: list[DatasetItemRead] = Field(default_factory=list)
    vector_annotation_sets: list[AnnotationSetRead] = Field(default_factory=list)
    raster_mask_annotation_sets: list[AnnotationSetRead] = Field(default_factory=list)


class MapInferenceCreate(ORMModel):
    dataset_item_ids: list[UUID] = Field(min_length=1)
    aoi_bbox: list[float] | None = None
    project_id: UUID | None = None
    mount_on_map: bool = False
    patch_size_px: int | None = Field(default=None, ge=64, le=4096)
    stride_px: int | None = Field(default=None, ge=32, le=4096)
    max_patches_per_item: int | None = Field(default=None, ge=1, le=4096)
    model_id: UUID

    def to_inference_job(self, *, map_id: UUID) -> InferenceJobCreate:
        return InferenceJobCreate(
            model_id=self.model_id,
            dataset_item_ids=self.dataset_item_ids,
            project_id=self.project_id,
            map_id=map_id,
            mount_on_map=self.mount_on_map,
            aoi_bbox=self.aoi_bbox,
            patch_size_px=self.patch_size_px,
            stride_px=self.stride_px,
            max_patches_per_item=self.max_patches_per_item,
        )


class MapInferenceResponse(ORMModel):
    job: JobRead
