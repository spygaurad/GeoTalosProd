import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        Index("idx_annotations_org", "organization_id"),
        Index("idx_annotations_dataset_item", "dataset_item_id"),
        Index("idx_annotations_stac_item", "stac_item_id"),
        Index("idx_annotations_geometry", "geometry", postgresql_using="gist"),
        Index("idx_annotations_properties", "properties", postgresql_using="gin"),
        Index("idx_annotations_track", "track_id"),
        Index("idx_annotations_label", "label"),
        Index("idx_annotations_is_current", "is_current"),
        Index("idx_annotations_status", "status"),
        Index("idx_annotations_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id", ondelete="CASCADE"), nullable=True
    )
    stac_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))
    pixel_coords: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("label_schemas.id"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default="manual")
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id"), nullable=True
    )
    track_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracked_objects.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="draft")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, server_default="1")
    is_current: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id"), nullable=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
