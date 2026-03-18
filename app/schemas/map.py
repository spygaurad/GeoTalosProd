from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse
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
