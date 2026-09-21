from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import ORMModel


def _validate_bbox(bbox: list[float]) -> list[float]:
    minx, miny, maxx, maxy = bbox
    if minx >= maxx or miny >= maxy:
        raise ValueError("bbox must be [minx, miny, maxx, maxy] with min < max")
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise ValueError("bbox must be within EPSG:4326 bounds")
    return bbox


class EmbeddingCreateRequest(ORMModel):
    """Generate + store an embedding for one bbox/mask selection.

    Synchronous — a single crop + one call to the embed model, not a bulk job.
    """

    model_id: UUID
    dataset_item_id: UUID
    geometry: dict[str, Any] = Field(
        description="GeoJSON geometry (EPSG:4326) of the bbox or mask the user selected. "
        "Only its bounding box is used to crop the source patch."
    )
    annotation_id: UUID | None = Field(
        default=None,
        description="If the selection is already a saved Annotation, link the embedding to it "
        "and inherit its class_id (an explicit class_id is ignored when this is set).",
    )
    class_id: UUID | None = Field(
        default=None,
        description="Annotation class this embedding represents. Ignored if annotation_id is set.",
    )
    crop_size_px: int = Field(default=256, ge=32, le=1024)


class EmbeddingRead(ORMModel):
    id: UUID
    organization_id: UUID
    model_id: UUID | None
    model_name: str
    embedding_dim: int
    dataset_item_id: UUID
    class_id: UUID | None
    annotation_id: UUID | None
    source_geometry: dict[str, Any]
    tile_tier: int
    created_at: datetime


class EmbeddingSearchRequest(ORMModel):
    """Nearest-neighbour search over the embedding bank.

    Always scoped to the reference embedding's ``model_name`` — vectors from
    different embedding models are never compared against each other.
    """

    embedding_id: UUID
    top_k: int = Field(default=10, ge=1, le=100)
    class_id: UUID | None = Field(default=None, description="Restrict candidates to this class.")
    dataset_id: UUID | None = Field(
        default=None, description="Restrict candidates to items belonging to this dataset."
    )
    exclude_self: bool = True


class EmbeddingSearchMatch(ORMModel):
    embedding_id: UUID
    dataset_item_id: UUID
    class_id: UUID | None
    annotation_id: UUID | None
    source_geometry: dict[str, Any]
    similarity: float = Field(description="1 - cosine distance. 1.0 = identical, 0.0 = orthogonal.")
    distance: float


class AOIScanRequest(ORMModel):
    """Async similarity scan over an AOI (runs as a Celery job — see
    ``POST /embeddings/aoi-scan``, which returns a ``job_id`` to poll).

    Tile size is derived entirely from the reference embedding's own scale
    tier (``geoops.scale``) — not configurable per request — so repeated
    scans of overlapping AOIs always produce the same tile grid and can
    reuse cached ``embedding_tiles`` rows instead of re-embedding.
    """

    reference_embedding_id: UUID
    dataset_item_ids: list[UUID] = Field(min_length=1, max_length=5)
    aoi_bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    crop_size_px: int = Field(default=256, ge=32, le=1024)
    top_k: int = Field(default=20, ge=1, le=200)
    min_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)
    max_patches: int = Field(
        default=200, ge=1, le=400,
        description="Cap on new tiles requiring a fresh model call — cached tiles from a "
        "prior scan don't count against this.",
    )
    output_class_id: UUID = Field(
        description="Annotation class the returned matches (top_k) are persisted under, as a "
        "new annotation_set (source_type='analysis') mounted as a map layer once the job "
        "completes.",
    )

    @model_validator(mode="after")
    def validate_aoi_bbox(self):
        if self.aoi_bbox is not None:
            _validate_bbox(self.aoi_bbox)
        return self


class AOIScanMatch(ORMModel):
    dataset_item_id: UUID
    bbox: list[float]
    similarity: float
    distance: float


class AOIScanResponse(ORMModel):
    """Shape of ``job.config["result"]`` once an AOI-scan job completes."""

    reference_embedding_id: UUID
    model_name: str
    patches_scanned: int
    patches_cached: int
    patches_embedded: int
    patches_truncated: bool = False
    matches: list[AOIScanMatch]


class AnomalyDetectionRequest(ORMModel):
    """Async anomaly scan over one annotation class (runs as a Celery job —
    see ``POST /embeddings/anomaly-scan``, which returns a ``job_id`` to poll).

    Only embeds annotations that already exist (created by a model run) —
    unlike ``aoi-scan``, this never tiles empty space. "Anomalous" means far
    (by cosine distance) from the centroid of its own scale-tier group, so a
    handful of annotations that look nothing like the rest of that class in
    this AOI surface first.
    """

    class_id: UUID
    model_id: UUID = Field(description="Embedding model used to embed each annotation.")
    dataset_item_ids: list[UUID] = Field(min_length=1, max_length=5)
    aoi_bbox: list[float] | None = Field(
        default=None, min_length=4, max_length=4,
        description="Optional further clip within the selected dataset items.",
    )
    min_group_size: int = Field(
        default=3, ge=2, le=50,
        description="A scale-tier group smaller than this is reported but not scored — "
        "distance-to-centroid isn't meaningful with too few peers.",
    )
    top_k: int = Field(default=20, ge=1, le=200)
    max_new_embeddings: int = Field(
        default=300, ge=1, le=1000,
        description="Cap on annotations requiring a fresh embed call — annotations already "
        "embedded by this model (from a prior run) don't count against this.",
    )
    output_class_id: UUID = Field(
        description="Annotation class flagged anomalies (top_k) are persisted under, as a new "
        "annotation_set (source_type='analysis') mounted as a map layer once the job "
        "completes. Independent of `class_id` above — that's the class being *scanned*, "
        "this is the class the *results* get tagged with, and the two may differ "
        "(e.g. scan 'palm_tree' detections, tag anomalies as a generic 'review_flag' class).",
    )

    @model_validator(mode="after")
    def validate_aoi_bbox(self):
        if self.aoi_bbox is not None:
            _validate_bbox(self.aoi_bbox)
        return self


class AnomalyGroup(ORMModel):
    tile_tier: int
    group_size: int
    scored: bool = Field(description="False when group_size < min_group_size — reported, not scored.")


class AnomalyMatch(ORMModel):
    annotation_id: UUID
    dataset_item_id: UUID
    tile_tier: int
    bbox: list[float]
    anomaly_score: float = Field(description="Cosine distance to its group's centroid. Higher = more anomalous.")
    similarity_to_group: float = Field(description="1 - anomaly_score.")


class AnomalyDetectionResponse(ORMModel):
    """Shape of ``job.config["result"]`` once an anomaly-detection job completes."""

    class_id: UUID
    model_name: str
    annotations_scanned: int
    annotations_cached: int
    annotations_embedded: int
    annotations_truncated: bool = False
    errors_total: int = 0
    groups: list[AnomalyGroup]
    anomalies: list[AnomalyMatch]
