import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AIModel(Base):
    __tablename__ = "ai_models"
    __table_args__ = (
        Index("idx_ai_models_org", "organization_id"),
        Index("idx_ai_models_backbone_model", "backbone_model_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    framework: Mapped[str | None] = mapped_column(String(50), nullable=True)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    endpoint_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    request_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    auth_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # S3 URI of a head-only checkpoint produced by a finetune_model job, meant
    # to be overlaid onto a shared backbone by yolo-service. Unset for models
    # backed purely by an external endpoint_url.
    artifact_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # For a per-class head row: the shared backbone it overlays onto. NULL for
    # a backbone row itself and for plain HTTP-endpoint inference models.
    backbone_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    annotation_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_schemas.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization", back_populates="ai_models")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    annotation_schema: Mapped["AnnotationSchema | None"] = relationship(
        "AnnotationSchema", foreign_keys=[annotation_schema_id]
    )
    jobs: Mapped[list["Job"]] = relationship("Job", back_populates="model")
    class_mappings: Mapped[list["ModelClassMapping"]] = relationship(
        "ModelClassMapping",
        back_populates="model",
        cascade="all, delete-orphan",
    )
    backbone: Mapped["AIModel | None"] = relationship(
        "AIModel", remote_side=[id], foreign_keys=[backbone_model_id]
    )
