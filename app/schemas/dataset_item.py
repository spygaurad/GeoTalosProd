from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class DatasetItemRead(ORMModel):
    id: UUID
    dataset_id: UUID
    organization_id: UUID
    stac_item_id: str
    stac_collection_id: str
    s3_uri: str
    filename: str
    geometry: dict | None
    item_datetime: datetime | None
    properties_cache: dict | None
    is_active: bool
    created_at: datetime


DatasetItemListResponse = PaginatedResponse[DatasetItemRead]


class DatasetItemTileConfig(ORMModel):
    """Stable identifiers for the frontend to build tile requests."""

    stac_item_id: str
    dataset_id: UUID
    # Tile URL template pointing at the API proxy — the frontend substitutes
    # {z}, {x}, {y} and appends any titiler render params (assets, rescale…)
    tile_url_template: str = Field(
        description="URL template for raster tiles. Substitute {z}/{x}/{y}."
    )


class DatasetItemPatchGenerateRequest(ORMModel):
    patch_size_px: int = Field(ge=64, le=4096)
    stride_px: int | None = Field(default=None, ge=32, le=4096)
    max_patches: int = Field(default=1024, ge=1, le=4096)


class DatasetItemPatch(ORMModel):
    patch_id: str
    patch_index: int
    x: int
    y: int
    width_px: int
    height_px: int
    bbox: list[float]


class DatasetItemPatchGenerateResponse(ORMModel):
    dataset_id: UUID
    item_id: UUID
    total_patches: int
    capped: bool
    patches: list[DatasetItemPatch]
