from uuid import UUID

from pydantic import BaseModel, Field


class MultiDatasetTileRequest(BaseModel):
    """Request body for registering a mosaic spanning multiple datasets."""

    dataset_ids: list[UUID] = Field(..., min_length=1)
    assets: str | None = Field(
        default=None,
        description="Comma-separated asset names (e.g. 'data' or 'visual'). Defaults to titiler auto-detection.",
    )
    preset: str | None = Field(
        default=None,
        description="Rendering preset name (e.g. 'natural_color', 'ndvi').",
    )
    rescale: str | None = Field(
        default=None,
        description="Rescale range for uint16 data (e.g. '0,10000').",
    )
    asset_bidx: str | None = Field(
        default=None,
        description="Band selection (e.g. 'data|1,2,3').",
    )


class MultiItemTileRequest(BaseModel):
    """Request body for registering a mosaic of specific dataset items."""

    item_ids: list[UUID] = Field(..., min_length=1)
    assets: str | None = Field(
        default=None,
        description="Comma-separated asset names.",
    )
    preset: str | None = Field(
        default=None,
        description="Rendering preset name.",
    )
    rescale: str | None = Field(
        default=None,
        description="Rescale range for uint16 data (e.g. '0,10000').",
    )
    asset_bidx: str | None = Field(
        default=None,
        description="Band selection (e.g. 'data|1,2,3').",
    )
