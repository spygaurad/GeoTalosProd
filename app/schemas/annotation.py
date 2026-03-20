from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.core.geometry import serialize_geometry
from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationCreate(ORMModel):
    class_id: UUID
    geometry: dict
    confidence: float | None = None
    properties: dict | None = None


class AnnotationUpdate(ORMModel):
    class_id: UUID | None = None
    geometry: dict | None = None
    confidence: float | None = None
    properties: dict | None = None


class AnnotationRead(ORMModel):
    id: UUID
    annotation_set_id: UUID
    class_id: UUID
    geometry: dict
    confidence: float | None
    properties: dict | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @field_validator("geometry", mode="before")
    @classmethod
    def _coerce_geometry(cls, value: Any) -> dict | None:
        return serialize_geometry(value)


AnnotationListResponse = PaginatedResponse[AnnotationRead]
