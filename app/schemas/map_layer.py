from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.core.enums import MapLayerSourceType, MapLayerType
from app.schemas.common import ORMModel, PaginatedResponse

_SOURCE_TYPES = set(MapLayerSourceType)
_LAYER_TYPES = set(MapLayerType)


class MapLayerCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    layer_type: str = Field(min_length=1, max_length=50)
    source_type: str = Field(min_length=1, max_length=50)

    # Exactly one of these must be set depending on source_type
    dataset_id: UUID | None = None
    stac_item_id: str | None = Field(default=None, max_length=255)
    tile_service_url: str | None = Field(default=None, max_length=500)
    tile_source_id: UUID | None = None

    source_config: dict | None = None
    style_id: UUID | None = None
    style_override: dict | None = None
    time_config: dict | None = None
    z_index: int = 0
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    min_zoom: int | None = Field(default=None, ge=0, le=24)
    max_zoom: int | None = Field(default=None, ge=0, le=24)

    @model_validator(mode="after")
    def _validate_layer(self) -> "MapLayerCreate":
        # layer_type
        if self.layer_type not in _LAYER_TYPES:
            raise ValueError(f"layer_type must be one of {sorted(_LAYER_TYPES)}")

        # source_type + exclusive source fields
        st = self.source_type
        if st not in _SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {sorted(_SOURCE_TYPES)}")
        if st == "dataset":
            if self.dataset_id is None:
                raise ValueError("dataset_id is required when source_type is 'dataset'")
            if self.stac_item_id or self.tile_service_url:
                raise ValueError("Only dataset_id may be set when source_type is 'dataset'")
        elif st == "stac_item":
            if self.stac_item_id is None:
                raise ValueError("stac_item_id is required when source_type is 'stac_item'")
            if self.dataset_id or self.tile_service_url:
                raise ValueError("Only stac_item_id may be set when source_type is 'stac_item'")
        elif st == "tile_service":
            if self.tile_service_url is None and self.tile_source_id is None:
                raise ValueError("tile_service_url or tile_source_id is required when source_type is 'tile_service'")
            if self.dataset_id or self.stac_item_id:
                raise ValueError("Only tile_service_url/tile_source_id may be set when source_type is 'tile_service'")

        # zoom range
        if self.min_zoom is not None and self.max_zoom is not None:
            if self.min_zoom > self.max_zoom:
                raise ValueError("min_zoom must be less than or equal to max_zoom")

        return self


class MapLayerUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    source_config: dict | None = None
    style_id: UUID | None = None
    style_override: dict | None = None
    time_config: dict | None = None
    # Non-nullable DB columns — typed Optional only so Pydantic treats them as
    # "not provided" when absent from the request body. Explicit null is rejected
    # by the validator below.
    z_index: int | None = None
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    min_zoom: int | None = Field(default=None, ge=0, le=24)
    max_zoom: int | None = Field(default=None, ge=0, le=24)

    @model_validator(mode="after")
    def _validate_update(self) -> "MapLayerUpdate":
        # Reject explicit null for NOT NULL DB columns
        for field in ("z_index", "visible", "opacity"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"'{field}' cannot be set to null")

        # zoom range (only when both are explicitly provided)
        mn = self.min_zoom
        mx = self.max_zoom
        if mn is not None and mx is not None and mn > mx:
            raise ValueError("min_zoom must be less than or equal to max_zoom")

        return self


class MapLayerRead(ORMModel):
    id: UUID
    map_id: UUID
    name: str
    layer_type: str
    source_type: str
    dataset_id: UUID | None
    stac_item_id: str | None
    tile_service_url: str | None
    tile_source_id: UUID | None
    source_config: dict | None
    style_id: UUID | None
    style_override: dict | None
    time_config: dict | None
    z_index: int
    visible: bool
    opacity: float
    min_zoom: int | None
    max_zoom: int | None
    created_at: datetime
    updated_at: datetime


MapLayerListResponse = PaginatedResponse[MapLayerRead]


class MapLayerReorderRequest(ORMModel):
    """Ordered list of layer IDs — first element becomes z_index 0 (bottom)."""

    layer_ids: list[UUID] = Field(min_length=1)
