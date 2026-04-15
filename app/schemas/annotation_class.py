from datetime import datetime
from uuid import UUID

from pydantic import Field, computed_field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationClassCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: UUID | None = None
    path: str | None = None
    style_id: UUID | None = None
    properties: dict | None = None


class AnnotationClassUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: UUID | None = None
    path: str | None = None
    style_id: UUID | None = None
    properties: dict | None = None


class EmbeddedStyle(ORMModel):
    """Style definition embedded inside AnnotationClassRead."""
    id: UUID
    name: str
    type: str
    definition: dict


class AnnotationClassRead(ORMModel):
    id: UUID
    schema_id: UUID
    parent_id: UUID | None
    name: str
    path: str | None
    style_id: UUID | None
    style: EmbeddedStyle | None = None   # eagerly loaded via .style relationship
    properties: dict | None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def description(self) -> str | None:
        """Expose class description from metadata as a first-class response field."""
        if not isinstance(self.properties, dict):
            return None
        raw = self.properties.get("description")
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        return text or None


class ClassStyleUpsert(ORMModel):
    """
    Upsert payload for PATCH /annotation-schemas/{schema_id}/classes/{class_id}/style.
    Creates a new Style record if the class has no style_id; updates the existing
    Style in-place if one already exists.
    """
    name: str | None = Field(default=None, max_length=255)
    type: str | None = Field(default=None, max_length=50)
    definition: dict = Field(
        ...,
        description="Full style definition: fillColor, strokeColor, strokeWidth, fillOpacity, …",
    )


AnnotationClassListResponse = PaginatedResponse[AnnotationClassRead]
