from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel

from geoops.export_common import SkippedSet

__all__ = [
    "SkippedSet",
    "YoloExportPreviewRequest",
    "ClassCount",
    "YoloExportPreviewResponse",
    "YoloFinetuneRequest",
    "YoloFinetuneResult",
]


class YoloExportPreviewRequest(ORMModel):
    """Validate + preview the training corpus an AnnotationSetCollection would
    export to, without touching TiTiler, S3, or yolo-service.

    Cheap, synchronous, safe to call repeatedly while the caller decides
    which collection/classes to actually train on.
    """

    annotation_set_collection_id: UUID


class ClassCount(ORMModel):
    class_id: UUID
    name: str
    annotation_count: int
    dataset_item_count: int = Field(description="Distinct dataset_items this class appears on.")


class YoloExportPreviewResponse(ORMModel):
    annotation_set_collection_id: UUID
    schema_id: UUID
    geometry_types: list[str] = Field(description="AnnotationSchema.geometry_types — informs detect vs segment eligibility.")
    verified_sets_included: int
    sets_skipped: list[SkippedSet]
    dataset_item_count: int = Field(description="Distinct dataset_items contributing at least one eligible annotation.")
    total_annotations: int
    annotations_skipped_no_item: int = Field(
        description="Verified annotations excluded because neither the annotation nor its "
        "(dataset-wide) set carries a dataset_item_id — nothing to crop a training patch "
        "from. Common for annotations verified before annotations.dataset_item_id existed; "
        "resolves itself for annotations verified going forward.",
    )
    annotations_skipped_inactive_item: int = Field(
        description="Verified annotations excluded because their resolved dataset_item is "
        "no longer active (superseded/removed source file).",
    )
    classes: list[ClassCount]
    ready: bool = Field(description="False if there are zero eligible annotations to train on.")


class YoloFinetuneRequest(ORMModel):
    """Export a verified AnnotationSetCollection to a YOLO dataset in S3
    (async job — see ``POST /yolo/finetune``, which returns a ``job_id`` to
    poll via ``GET /api/jobs/{job_id}``).

    Today this only runs the export (images + labels + data.yaml to S3).
    The hyperparameters below are accepted and stored on the job now so the
    endpoint contract doesn't change again once yolo-service exists to
    actually consume them.
    """

    annotation_set_collection_id: UUID
    task: Literal["detect", "segment"] = "detect"
    val_split: float = Field(default=0.15, ge=0.05, le=0.5)

    # Accepted now, not yet acted on (yolo-service doesn't exist yet).
    backbone_id: str | None = Field(default=None, description="Shared backbone to fine-tune against.")
    epochs: int = Field(default=50, ge=1, le=1000)
    freeze: int | list[str] = Field(
        default=10,
        description="Ultralytics `freeze` value — an int (freeze the first N layers, "
        "protecting the backbone/neck) or a list of module names for finer-grained control.",
    )
    result_model_name: str | None = Field(
        default=None, description="Name for the resulting AIModel row. Defaults to the collection's name."
    )


class YoloFinetuneResult(ORMModel):
    """Shape of ``job.config["result"]`` once a finetune_model job completes."""

    s3_prefix: str
    task: str
    classes: list[dict]
    train_items: int
    val_items: int
    sets_skipped: list[SkippedSet]
    errors: list[str]
    training_triggered: bool
    note: str
