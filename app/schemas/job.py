from datetime import datetime
from uuid import UUID

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
