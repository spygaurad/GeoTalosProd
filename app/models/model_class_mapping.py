import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ModelClassMapping(Base):
    __tablename__ = "model_class_mappings"
    __table_args__ = (
        UniqueConstraint("model_id", "model_label", name="uq_model_class_mappings_model_label"),
        Index("idx_model_class_mappings_model", "model_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="CASCADE"), nullable=False
    )
    model_label: Mapped[str] = mapped_column(String(255), nullable=False)
    annotation_class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_classes.id", ondelete="CASCADE"), nullable=False
    )
    confidence_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    model: Mapped["AIModel"] = relationship("AIModel", back_populates="class_mappings")
    annotation_class: Mapped["AnnotationClass"] = relationship("AnnotationClass")
