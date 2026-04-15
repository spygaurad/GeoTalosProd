from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSetCreate(ORMModel):
    map_id: UUID | None = None
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    stac_item_id: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetUpdate(ORMModel):
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    stac_item_id: str | None = Field(default=None, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetRead(ORMModel):
    id: UUID
    map_id: UUID | None
    schema_id: UUID | None
    dataset_id: UUID | None
    stac_item_id: str | None
    name: str
    description: str | None
    created_by_user_id: UUID | None
    created_by_job_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


AnnotationSetListResponse = PaginatedResponse[AnnotationSetRead]
