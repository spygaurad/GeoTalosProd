from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import ORMModel

from geoops.export_common import SkippedSet


class DinoExportPreviewRequest(ORMModel):
    """Validate + preview the training corpus for ONE class-specific head,
    without touching TiTiler, S3, or geoops-service.

    Unlike the YOLO tier (one multi-class model per collection), the DINOv2
    tier trains one head per class — so preview/finetune are scoped to a
    single class_id within the collection, picked manually in the UI.
    """

    annotation_set_collection_id: UUID
    class_id: UUID


class DinoExportPreviewResponse(ORMModel):
    annotation_set_collection_id: UUID
    class_id: UUID
    class_name: str
    schema_id: UUID
    geometry_types: list[str]
    verified_sets_included: int
    sets_skipped: list[SkippedSet]
    dataset_item_count: int = Field(description="Distinct dataset_items contributing at least one eligible annotation of this class.")
    total_annotations: int
    annotations_skipped_no_item: int = Field(
        description="Verified annotations of this class excluded because neither the "
        "annotation nor its (dataset-wide) set carries a dataset_item_id.",
    )
    annotations_skipped_inactive_item: int
    ready: bool = Field(description="False if there are zero eligible annotations of this class to train on.")


class DinoFinetuneRequest(ORMModel):
    """Train one class-specific head on a shared, frozen DINOv2 backbone
    (async job — see ``POST /dino/finetune``, poll ``GET /api/jobs/{job_id}``).

    Today this only runs the export (crops + normalized bbox/mask targets +
    manifest.json to S3). Calling geoops-service to actually train the head
    is a follow-up step, not yet implemented — see YoloFinetuneRequest's
    equivalent note for the same reasoning.
    """

    annotation_set_collection_id: UUID
    class_id: UUID
    task: Literal["detect", "segment", "classify"] = "detect"
    # Auto three-way split by dataset_item (deterministic hash, no leakage
    # across splits). eval_split is tracked during/after training; test_split
    # is fully held out and only touched by a final /eval call. Remainder
    # goes to train. Manual negative-class-from-another-set support is a
    # later feature — for now every item here is a positive example.
    eval_split: float = Field(default=0.15, ge=0.05, le=0.4)
    test_split: float = Field(default=0.15, ge=0.05, le=0.4)

    backbone_model_id: UUID | None = Field(
        default=None,
        description="Existing 'backbone' ai_models row to fine-tune against. If unset, "
        "geoops-service falls back to its default DINOv2 backbone.",
    )
    epochs: int = Field(default=20, ge=1, le=2000)
    lr: float = Field(default=1e-3, gt=0, le=1.0)
    result_model_name: str | None = Field(
        default=None, description="Name for the resulting per-class head AIModel row."
    )


class DinoFinetuneResult(ORMModel):
    """Shape of ``job.config["result"]`` once a finetune_model (dino tier) job completes.

    ``training_triggered=False`` cases (zero eligible items, or task="segment"
    — not implemented on geoops-service yet) omit the fields below
    ``errors``; a successful detect run adds ``ai_model_id``,
    ``backbone_model_id``, ``train_result`` (geoops-service /train response),
    and ``eval_result`` (geoops-service /eval response on the test split).
    """

    s3_prefix: str
    task: str
    class_id: str
    class_name: str
    train_items: int
    eval_items: int
    test_items: int
    sets_skipped: list[SkippedSet]
    errors: list[str]
    annotations_skipped_no_aoi: int = Field(
        default=0,
        description="Verified annotations of this class excluded because they aren't tagged to a "
        "tracked AOI — training requires an exhaustively-labeled AOI so empty tiles can be trusted "
        "as real negatives.",
    )
    patch_span_m: float | None = Field(
        default=None, description="Fixed real-world tile size (meters) used for both training crops and scan tiles."
    )
    training_triggered: bool
    ai_model_id: str | None = None
    backbone_model_id: str | None = None
    train_result: dict | None = None
    eval_result: dict | None = None
    note: str | None = None
    error: str | None = None
