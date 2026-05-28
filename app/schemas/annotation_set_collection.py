from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.annotation_set import AnnotationSetRead
from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSetCollectionCreate(ORMModel):
    schema_id: UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetCollectionUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetCollectionRead(ORMModel):
    id: UUID
    organization_id: UUID
    schema_id: UUID
    name: str
    description: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class AnnotationSetCollectionItemCreate(ORMModel):
    annotation_set_id: UUID


class AnnotationSetCollectionItemRead(ORMModel):
    collection_id: UUID
    annotation_set_id: UUID
    linked_at: datetime
    linked_by: UUID | None


AnnotationSetCollectionListResponse = PaginatedResponse[AnnotationSetCollectionRead]
AnnotationSetCollectionItemListResponse = PaginatedResponse[AnnotationSetRead]
