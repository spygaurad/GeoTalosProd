import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SpatialBookmark(Base):
    __tablename__ = "spatial_bookmarks"
    __table_args__ = (
        Index("idx_spatial_bookmarks_org", "organization_id"),
        Index("idx_spatial_bookmarks_user", "user_id"),
        Index("idx_spatial_bookmarks_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # WGS84 Point — map center longitude/latitude
    center: Mapped[object] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=False
    )
    zoom: Mapped[float] = mapped_column(Float, nullable=False)
    bearing: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    pitch: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # ["satellite-layer", "annotations-layer"]
    visible_layers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="[]")
    # {"label": "fire", "status": "active"}
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
