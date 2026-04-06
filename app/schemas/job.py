from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel


class JobRead(ORMModel):
    id: UUID
    organization_id: UUID
    type: str
    status: str
    config: dict | None
    input_refs: list | None
    processed_items: int
    total_items: int
    failed_items: int
    progress: float | None
    logs: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InferenceJobCreate(ORMModel):
    model_id: UUID
    dataset_item_ids: list[UUID] = Field(min_length=1)
    project_id: UUID | None = None
    map_id: UUID | None = None
    mount_on_map: bool = False
