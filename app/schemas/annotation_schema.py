from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSchemaCreate(ORMModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    version: int = Field(default=1, ge=1)
    geometry_types: list[str] = Field(min_length=1)
    properties_schema: dict | None = None


class AnnotationSchemaUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    version: int | None = Field(default=None, ge=1)
    geometry_types: list[str] | None = None
    properties_schema: dict | None = None


class AnnotationSchemaRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    version: int
    geometry_types: list[str]
    properties_schema: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


AnnotationSchemaListResponse = PaginatedResponse[AnnotationSchemaRead]
