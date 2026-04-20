import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MapAnnotationSet(Base):
    __tablename__ = "map_annotation_sets"

    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("maps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    annotation_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_sets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    opacity: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("styles.id", ondelete="SET NULL"), nullable=True
    )
    style_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    mounted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    map: Mapped["Map"] = relationship("Map", back_populates="annotation_set_mounts")
    annotation_set: Mapped["AnnotationSet"] = relationship(
        "AnnotationSet", back_populates="map_links"
    )
    style: Mapped["Style | None"] = relationship("Style")
