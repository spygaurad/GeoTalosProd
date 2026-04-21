from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.schemas.common import ORMModel, PaginatedResponse

_ROLES = {"reference", "aoi", "sketch"}


class FeatureLayerCreate(ORMModel):
    layer_name: str = Field(min_length=1, max_length=255)
    role: str = Field(default="reference", max_length=30)
    geometry: dict
    properties: dict | None = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in _ROLES:
            raise ValueError(f"role must be one of {sorted(_ROLES)}")
        return v


class FeatureLayerRead(ORMModel):
    id: UUID
    organization_id: UUID
    layer_name: str
    role: str = "reference"
    geometry: dict | None
    properties: dict | None
    created_by: UUID | None
    created_at: datetime


FeatureLayerListResponse = PaginatedResponse[FeatureLayerRead]


class FeatureLayerBulkCreate(ORMModel):
    features: list[FeatureLayerCreate] = Field(min_length=1, max_length=10000)
