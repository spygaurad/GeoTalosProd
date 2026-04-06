import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (Index("idx_datasets_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    stac_collection_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    geometry: Mapped[object | None] = mapped_column(Geometry("POLYGON", srid=4326))
    temporal_extent: Mapped[object | None] = mapped_column(TSTZRANGE)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
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

    organization: Mapped["Organization"] = relationship("Organization", back_populates="datasets")
    map_layers: Mapped[list["MapLayer"]] = relationship("MapLayer", back_populates="dataset")
    annotation_sets: Mapped[list["AnnotationSet"]] = relationship(
        "AnnotationSet",
        back_populates="dataset",
    )
    items: Mapped[list["DatasetItem"]] = relationship(
        "DatasetItem",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )
    project_links: Mapped[list["ProjectDataset"]] = relationship(
        "ProjectDataset",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
