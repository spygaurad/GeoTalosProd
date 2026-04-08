import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AnnotationSchema(Base):
    __tablename__ = "annotation_schemas"
    __table_args__ = (
        Index("idx_annotation_schemas_org", "organization_id"),
        UniqueConstraint("organization_id", "name", "version", name="uq_annotation_schemas_org_name_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    geometry_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    properties_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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

    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="annotation_schemas"
    )
    classes: Mapped[list["AnnotationClass"]] = relationship(
        "AnnotationClass",
        back_populates="schema",
        cascade="all, delete-orphan",
    )
    annotation_sets: Mapped[list["AnnotationSet"]] = relationship(
        "AnnotationSet",
        back_populates="schema",
    )
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
