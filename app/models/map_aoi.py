import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MapAOI(Base):
    __tablename__ = "map_aois"
    __table_args__ = (
        Index("idx_map_aois_map", "map_id"),
        Index("idx_map_aois_org", "organization_id"),
        Index("idx_map_aois_visible", "map_id", "visible"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maps.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bbox_4326: Mapped[list] = mapped_column(JSONB, nullable=False)
    geometry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    selection_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    render_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    temporal_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    opacity: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
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

    map: Mapped["Map"] = relationship("Map", back_populates="aois")
    organization: Mapped["Organization"] = relationship("Organization")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
