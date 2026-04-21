import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AnnotationSet(Base):
    __tablename__ = "annotation_sets"
    __table_args__ = (
        Index("idx_annotation_sets_org", "organization_id"),
        Index("idx_annotation_sets_schema", "schema_id"),
        Index("idx_annotation_sets_dataset", "dataset_id"),
        Index("idx_annotation_sets_dataset_item", "dataset_item_id"),
        Index("idx_annotation_sets_source_type", "source_type"),
        Index("idx_annotation_sets_model", "model_id"),
        Index("idx_annotation_sets_created_by_user", "created_by_user_id"),
        Index("idx_annotation_sets_job", "job_id"),
        CheckConstraint(
            "(created_by_user_id IS NOT NULL) OR (job_id IS NOT NULL)",
            name="ck_annotation_sets_creator",
        ),
        CheckConstraint(
            "source_type IN ('manual', 'model', 'import', 'analysis')",
            name="ck_annotation_sets_source_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # schema_id is NOT NULL as of migration 047 (Stage 1, unified platform
    # plan). Every annotation set carries a schema so raster masks have class
    # semantics and vector sets have per-class styling/legend data.
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_schemas.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default="manual")
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    raster_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    schema: Mapped["AnnotationSchema"] = relationship("AnnotationSchema", back_populates="annotation_sets")
    dataset: Mapped["Dataset | None"] = relationship("Dataset", back_populates="annotation_sets")
    dataset_item: Mapped["DatasetItem | None"] = relationship("DatasetItem")
    model: Mapped["AIModel | None"] = relationship("AIModel")
    creator_user: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_user_id])
    job: Mapped["Job | None"] = relationship("Job", back_populates="annotation_sets", foreign_keys=[job_id])
    project_links: Mapped[list["ProjectAnnotationSet"]] = relationship(
        "ProjectAnnotationSet",
        back_populates="annotation_set",
        cascade="all, delete-orphan",
    )
    map_links: Mapped[list["MapAnnotationSet"]] = relationship(
        "MapAnnotationSet",
        back_populates="annotation_set",
        cascade="all, delete-orphan",
    )
    annotations: Mapped[list["Annotation"]] = relationship(
        "Annotation",
        back_populates="annotation_set",
        cascade="all, delete-orphan",
    )
