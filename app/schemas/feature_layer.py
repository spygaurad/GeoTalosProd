from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel, PaginatedResponse


class FeatureLayerCreate(ORMModel):
    layer_name: str = Field(min_length=1, max_length=255)
    geometry: dict
    properties: dict | None = None


class FeatureLayerRead(ORMModel):
    id: UUID
    organization_id: UUID
    layer_name: str
    geometry: dict | None
    properties: dict | None
    created_by: UUID | None
    created_at: datetime


FeatureLayerListResponse = PaginatedResponse[FeatureLayerRead]


class FeatureLayerBulkCreate(ORMModel):
    features: list[FeatureLayerCreate] = Field(min_length=1, max_length=10000)
