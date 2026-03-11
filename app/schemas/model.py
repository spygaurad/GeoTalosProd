from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from app.schemas.common import ORMModel, PaginatedResponse


class ModelCreate(ORMModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    organization_id: UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    model_type: str = Field(alias="type", min_length=1, max_length=50)
    version: str | None = None
    artifact_uri: str | None = None
    config: dict = Field(default_factory=dict)
    created_by: UUID | None = None


class ModelUpdate(ORMModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    model_type: str | None = Field(default=None, alias="type", min_length=1, max_length=50)
    version: str | None = None
    artifact_uri: str | None = None
    config: dict | None = None


class ModelRead(ORMModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    model_type: str = Field(alias="type")
    version: str | None
    artifact_uri: str | None
    config: dict = Field(default_factory=dict)
    created_by: UUID | None
    created_at: datetime


class ModelListResponse(PaginatedResponse):
    items: list[ModelRead]
