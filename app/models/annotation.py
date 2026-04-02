import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Float, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        Index("idx_annotations_created_by_user", "created_by_user_id"),
        Index("idx_annotations_created_by_job", "created_by_job_id"),
        CheckConstraint(
            "(created_by_user_id IS NOT NULL AND created_by_job_id IS NULL) OR "
            "(created_by_user_id IS NULL AND created_by_job_id IS NOT NULL)",
            name="ck_annotations_creator",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v7()")
    )
    annotation_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_sets.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_classes.id"), nullable=False
    )
    geometry: Mapped[object] = mapped_column(Geometry("Geometry", srid=4326), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    properties: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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

    annotation_set: Mapped["AnnotationSet"] = relationship("AnnotationSet", back_populates="annotations")
    cls: Mapped["AnnotationClass"] = relationship("AnnotationClass", back_populates="annotations")
    creator_user: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_user_id])
    creator_job: Mapped["Job | None"] = relationship("Job", foreign_keys=[created_by_job_id])
