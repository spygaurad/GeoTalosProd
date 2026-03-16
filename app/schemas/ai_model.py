from datetime import datetime
from uuid import UUID

from pydantic import Field, computed_field

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
    # auth_config is intentionally excluded from responses; presence is surfaced via has_auth_config
    auth_config: dict | None = Field(default=None, exclude=True)
    input_schema: dict | None
    output_schema: dict | None
    config: dict | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @computed_field
    @property
    def has_auth_config(self) -> bool:
        return self.auth_config is not None


AIModelListResponse = PaginatedResponse[AIModelRead]
