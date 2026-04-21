from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel


class SAM3PromptPCS(BaseModel):
    """Concept Segmentation prompt: text phrases and/or image exemplars."""

    text_phrases: list[str] | None = Field(
        default=None,
        description="Noun phrases to segment, e.g. ['deforestation front', 'bare soil']",
    )
    exemplar_s3_uris: list[str] | None = Field(
        default=None,
        description="S3 URIs of image exemplars illustrating the concept",
    )

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.text_phrases and not self.exemplar_s3_uris:
            raise ValueError("PCS requires text_phrases or exemplar_s3_uris")
        return self


class SAM3PromptPVS(BaseModel):
    """Visual Segmentation prompt: points and/or boxes in EPSG:4326 lon/lat.

    The worker converts geographic coordinates to pixel coordinates using the
    raster's affine transform before calling SAM3.
    """

    points: list[list[float]] | None = Field(
        default=None, description="[[lon, lat], ...] in EPSG:4326"
    )
    point_labels: list[int] | None = Field(
        default=None,
        description="Per-point: 1 = foreground, 0 = background. Must match points length.",
    )
    boxes: list[list[float]] | None = Field(
        default=None,
        description="[[minx, miny, maxx, maxy], ...] in EPSG:4326",
    )

    @model_validator(mode="after")
    def _validate(self):
        if not self.points and not self.boxes:
            raise ValueError("PVS requires points or boxes")
        if self.points and self.point_labels and len(self.points) != len(self.point_labels):
            raise ValueError("points and point_labels must be the same length")
        return self


class SAM3InferenceRequest(BaseModel):
    model_id: UUID
    dataset_item_id: UUID
    annotation_set_name: str = Field(min_length=1, max_length=255)
    aoi_geometry: dict | None = Field(
        default=None,
        description="GeoJSON Polygon to clip inference to. None = full dataset item.",
    )
    task_type: Literal["pcs", "pvs"]
    prompt_pcs: SAM3PromptPCS | None = None
    prompt_pvs: SAM3PromptPVS | None = None
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    output_format: Literal["vector", "raster_cog"] = "vector"

    @model_validator(mode="after")
    def _prompt_matches_task(self):
        if self.task_type == "pcs" and not self.prompt_pcs:
            raise ValueError("task_type='pcs' requires prompt_pcs")
        if self.task_type == "pvs" and not self.prompt_pvs:
            raise ValueError("task_type='pvs' requires prompt_pvs")
        return self


class SAM3InferenceResponse(ORMModel):
    job_id: UUID
    annotation_set_id: UUID
    status: str = "pending"
