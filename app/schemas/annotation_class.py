from datetime import datetime
from uuid import UUID

from pydantic import Field

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


class AnnotationClassRead(ORMModel):
    id: UUID
    schema_id: UUID
    parent_id: UUID | None
    name: str
    path: str | None
    style_id: UUID | None
    properties: dict | None
    created_at: datetime
    updated_at: datetime


AnnotationClassListResponse = PaginatedResponse[AnnotationClassRead]
