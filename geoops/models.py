"""Embedding bank ORM model.

One row = one embedding vector for one bbox/mask selection on one dataset
item, produced by one embedding model. ``model_name`` + ``embedding_dim`` are
denormalized from the embed model's own response (not just looked up off
``ai_models.id``) so a bank entry stays self-describing even if the
``ai_models`` row is later edited or deleted, and so every similarity query
can scope its comparison set to "same model, same dimension" without a join.

The ``embedding`` column is an *unconstrained* pgvector ``vector`` (no fixed
dimension at the column level) — different embedding models legitimately
return different dimensions, and we never compare across models anyway.
"""
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        Index("idx_embeddings_org_model", "organization_id", "model_name"),
        Index("idx_embeddings_dataset_item", "dataset_item_id"),
        Index("idx_embeddings_class", "class_id"),
        Index("idx_embeddings_annotation", "annotation_id"),
        Index("idx_embeddings_org_tier", "organization_id", "model_name", "tile_tier"),
        # Cache key for annotation-tied embeddings (manual creates and the
        # anomaly-detection job both check this before embedding): one
        # annotation only ever needs one embedding per model. Partial so it
        # doesn't constrain the many rows with annotation_id IS NULL (plain
        # bbox selections with no saved annotation).
        Index(
            "uq_embeddings_annotation_model", "annotation_id", "model_id",
            unique=True, postgresql_where=text("annotation_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalized from the embed endpoint's own response — see module docstring.
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_classes.id", ondelete="SET NULL"), nullable=True
    )
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id", ondelete="SET NULL"), nullable=True
    )
    # GeoJSON geometry of the bbox/mask selection the embedding was computed
    # from, in EPSG:4326 — mirrors DatasetItem.geometry's plain-JSONB choice
    # rather than a PostGIS column, since nothing here needs spatial SQL on it.
    source_geometry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Real-world scale tier of source_geometry's bbox (see geoops/scale.py).
    # search() always filters candidates to the reference's own tier, so a
    # 5m object and a 500m object never rank as "similar" purely because
    # they were both squeezed into the same crop_size_px pixel grid.
    tile_tier: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Set when a batch job (e.g. anomaly detection) embedded this annotation
    # rather than a user clicking "embed" one at a time. Mutually informative
    # with created_by_user_id, not mutually exclusive by constraint — a row
    # can in principle have neither (e.g. future non-job automation).
    created_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship("Organization")
    model: Mapped["AIModel | None"] = relationship("AIModel")
    dataset_item: Mapped["DatasetItem"] = relationship("DatasetItem")
    cls: Mapped["AnnotationClass | None"] = relationship("AnnotationClass")
    annotation: Mapped["Annotation | None"] = relationship("Annotation")


class EmbeddingTile(Base):
    """Cache of grid-aligned patch embeddings produced by AOI scans.

    Deliberately separate from ``Embedding`` (the curated bank behind
    ``search()``): every row here is auto-generated coverage from a scan, not
    something a user or annotation chose, and there can be far more of them
    than curated embeddings. Keeping them apart means ``search()`` never has
    to filter out scan noise, and a scan can be re-run over an overlapping
    AOI without re-embedding tiles it already has — the unique constraint on
    (dataset_item_id, model_id, tile_tier, tile_col, tile_row) is the cache
    key.
    """

    __tablename__ = "embedding_tiles"
    __table_args__ = (
        UniqueConstraint(
            "dataset_item_id", "model_id", "tile_tier", "tile_col", "tile_row",
            name="uq_embedding_tiles_grid",
        ),
        Index("idx_embedding_tiles_org_model", "organization_id", "model_name"),
        Index("idx_embedding_tiles_dataset_item", "dataset_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id", ondelete="CASCADE"), nullable=False
    )
    tile_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    tile_col: Mapped[int] = mapped_column(Integer, nullable=False)
    tile_row: Mapped[int] = mapped_column(Integer, nullable=False)
    # GeoJSON bbox of this tile, EPSG:4326 — same plain-JSONB choice as
    # Embedding.source_geometry.
    bbox: Mapped[dict] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_by_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship("Organization")
    model: Mapped["AIModel | None"] = relationship("AIModel")
    dataset_item: Mapped["DatasetItem"] = relationship("DatasetItem")
    created_by_job: Mapped["Job"] = relationship("Job")
