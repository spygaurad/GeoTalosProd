from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import ORMModel
from app.schemas.job import JobRead


class InferenceBatchCreate(ORMModel):
    model_id: UUID
    map_id: UUID
    schema_id: UUID
    dataset_id: UUID | None = None
    stac_item_ids: list[str] | None = None
    params: dict | None = None
    set_name: str | None = Field(default=None, min_length=1, max_length=255)
    create_overlay_layer: bool = True
    auto_create_classes: bool = False

    @model_validator(mode="after")
    def _validate_inputs(self) -> "InferenceBatchCreate":
        has_dataset = self.dataset_id is not None
        has_items = bool(self.stac_item_ids)
        if has_dataset == has_items:
            raise ValueError("Provide exactly one of dataset_id or stac_item_ids")
        return self


class InferenceBatchJobResponse(ORMModel):
    job: JobRead
