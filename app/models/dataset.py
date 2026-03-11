import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (
        Index("idx_datasets_org", "organization_id"),
        Index("idx_datasets_project", "project_id"),
        Index("idx_datasets_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stac_collection_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    file_format: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="pending")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    temporal_extent_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_extent_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spatial_extent: Mapped[object | None] = mapped_column(Geometry("POLYGON", srid=4326))
    license: Mapped[str | None] = mapped_column(String, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    parent_dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list["DatasetItem"]] = relationship(
        "DatasetItem",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )
