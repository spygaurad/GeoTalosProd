from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class AIModelCreate(ORMModel):
    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    framework: str | None = None
    version: str | None = None
    type: str | None = None
    endpoint_url: str | None = None
    request_config: dict | None = None
    auth_config: dict | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    config: dict | None = None
    created_by: UUID | None = None


class AIModelUpdate(ORMModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    framework: str | None = None
    version: str | None = None
    type: str | None = None
    endpoint_url: str | None = None
    request_config: dict | None = None
    auth_config: dict | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    config: dict | None = None
    deleted_at: datetime | None = None


class AIModelRead(ORMModel):
    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    framework: str | None
    version: str | None
    type: str | None
    endpoint_url: str | None
    request_config: dict | None
    auth_config: dict | None
    input_schema: dict | None
    output_schema: dict | None
    config: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class AIModelListResponse(PaginatedResponse):
    items: list[AIModelRead]
