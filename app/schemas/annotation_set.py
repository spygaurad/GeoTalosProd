from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AnnotationSetCreate(ORMModel):
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    dataset_item_id: UUID | None = None
    source_type: str = Field(default="manual", pattern=r"^(manual|model|import|analysis)$")
    model_id: UUID | None = None
    job_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetLinkRequest(ORMModel):
    annotation_set_id: UUID


class AnnotationSetMountRequest(ORMModel):
    annotation_set_id: UUID
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z_index: int = 0
    style_id: UUID | None = None
    style_override: dict | None = None


class AnnotationSetMountUpdate(ORMModel):
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    z_index: int | None = None
    style_id: UUID | None = None
    style_override: dict | None = None


class AnnotationSetMountRead(ORMModel):
    map_id: UUID
    annotation_set_id: UUID
    visible: bool
    opacity: float
    z_index: int
    style_id: UUID | None
    style_override: dict | None
    mounted_at: datetime


class AnnotationSetMountListResponse(ORMModel):
    items: list[AnnotationSetMountRead]
    total: int


class AnnotationSetProjectLinkRead(ORMModel):
    project_id: UUID
    annotation_set_id: UUID
    linked_at: datetime
    linked_by: UUID | None


class AnnotationSetProjectLinkListResponse(ORMModel):
    items: list[AnnotationSetProjectLinkRead]
    total: int


class AnnotationSetUpdate(ORMModel):
    schema_id: UUID | None = None
    dataset_id: UUID | None = None
    dataset_item_id: UUID | None = None
    source_type: str | None = Field(default=None, pattern=r"^(manual|model|import|analysis)$")
    model_id: UUID | None = None
    job_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class AnnotationSetRead(ORMModel):
    id: UUID
    organization_id: UUID
    schema_id: UUID | None
    dataset_id: UUID | None
    dataset_item_id: UUID | None
    source_type: str
    model_id: UUID | None
    job_id: UUID | None
    name: str
    description: str | None
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


AnnotationSetListResponse = PaginatedResponse[AnnotationSetRead]
