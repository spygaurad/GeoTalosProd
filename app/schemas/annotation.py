from datetime import datetime
from uuid import UUID

from pydantic import Field, field_serializer

from app.core.geometry import serialize_geometry
from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationCreate(ORMModel):
    organization_id: UUID
    dataset_item_id: UUID | None = None
    stac_item_id: str | None = None
    geometry: dict | None = None
    pixel_coords: dict | None = None
    label: str | None = None
    label_schema_id: UUID | None = None
    confidence: float | None = None
    properties: dict = Field(default_factory=dict)
    source: str = Field(default="manual", min_length=1, max_length=50)
    model_id: UUID | None = None
    track_id: UUID | None = None
    status: str = Field(default="draft", min_length=1, max_length=50)
    tags: list[str] = Field(default_factory=list)
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    created_by: UUID | None = None


class AnnotationUpdate(ORMModel):
    geometry: dict | None = None
    pixel_coords: dict | None = None
    label: str | None = None
    label_schema_id: UUID | None = None
    confidence: float | None = None
    properties: dict | None = None
    source: str | None = Field(default=None, min_length=1, max_length=50)
    model_id: UUID | None = None
    track_id: UUID | None = None
    status: str | None = Field(default=None, min_length=1, max_length=50)
    tags: list[str] | None = None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    is_current: bool | None = None
    parent_version_id: UUID | None = None
    parent_id: UUID | None = None


class AnnotationRead(ORMModel):
    id: UUID
    organization_id: UUID
    dataset_item_id: UUID | None
    stac_item_id: str | None
    geometry: dict | None = None
    pixel_coords: dict | None
    label: str | None
    label_schema_id: UUID | None
    confidence: float | None
    properties: dict = Field(default_factory=dict)
    source: str
    model_id: UUID | None
    track_id: UUID | None
    status: str
    tags: list[str] = Field(default_factory=list)
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    version: int
    is_current: bool
    parent_version_id: UUID | None
    parent_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("geometry")
    def _serialize_geometry(self, value):
        return serialize_geometry(value)


class AnnotationListResponse(PaginatedResponse):
    items: list[AnnotationRead]
