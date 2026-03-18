import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AnnotationSet(Base):
    __tablename__ = "annotation_sets"
    __table_args__ = (
        Index("idx_annotation_sets_map", "map_id"),
        Index("idx_annotation_sets_schema", "schema_id"),
        Index("idx_annotation_sets_dataset", "dataset_id"),
        Index("idx_annotation_sets_stac_item", "stac_item_id"),
        Index("idx_annotation_sets_created_by_user", "created_by_user_id"),
        Index("idx_annotation_sets_created_by_job", "created_by_job_id"),
        CheckConstraint(
            "(created_by_user_id IS NOT NULL AND created_by_job_id IS NULL) OR "
            "(created_by_user_id IS NULL AND created_by_job_id IS NOT NULL)",
            name="ck_annotation_sets_creator",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maps.id", ondelete="CASCADE"), nullable=False
    )
    schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_schemas.id"), nullable=True
    )
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True
    )
    stac_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    map: Mapped["Map"] = relationship("Map", back_populates="annotation_sets")
    schema: Mapped["AnnotationSchema"] = relationship("AnnotationSchema", back_populates="annotation_sets")
    dataset: Mapped["Dataset | None"] = relationship("Dataset", back_populates="annotation_sets")
    creator_user: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_user_id])
    creator_job: Mapped["Job | None"] = relationship("Job", back_populates="annotation_sets", foreign_keys=[created_by_job_id])
    annotations: Mapped[list["Annotation"]] = relationship(
        "Annotation",
        back_populates="annotation_set",
        cascade="all, delete-orphan",
    )
