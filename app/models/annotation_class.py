import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

from app.db.base import Base


class LtreeType(UserDefinedType):
    """Maps to the PostgreSQL ltree extension type."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "ltree"


class AnnotationClass(Base):
    __tablename__ = "annotation_classes"
    __table_args__ = (
        Index("idx_annotation_classes_schema", "schema_id"),
        Index("idx_annotation_classes_parent", "parent_id"),
        Index("idx_annotation_classes_style", "style_id"),
        Index("idx_annotation_classes_path", "path", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_schemas.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_classes.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str | None] = mapped_column(LtreeType, nullable=True)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("styles.id", ondelete="SET NULL"), nullable=True
    )
    properties: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    schema: Mapped["AnnotationSchema"] = relationship("AnnotationSchema", back_populates="classes")
    parent: Mapped["AnnotationClass | None"] = relationship("AnnotationClass", remote_side=[id])
    style: Mapped["Style | None"] = relationship("Style")
    annotations: Mapped[list["Annotation"]] = relationship("Annotation", back_populates="cls")
