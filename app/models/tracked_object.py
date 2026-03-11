import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TrackedObject(Base):
    __tablename__ = "tracked_objects"
    __table_args__ = (
        Index("idx_tracked_objects_org", "organization_id"),
        Index("idx_tracked_objects_type_status", "object_type", "status"),
        Index("idx_tracked_objects_latest_geom", "latest_geometry", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="active")
    priority: Mapped[str | None] = mapped_column(String(50), nullable=True, server_default="medium")
    severity: Mapped[float | None] = mapped_column(nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(nullable=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracked_objects.id"), nullable=True
    )
    alert_threshold: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_count: Mapped[int] = mapped_column(
        nullable=False,
        server_default="0",
    )
    latest_geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))
    cumulative_geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
