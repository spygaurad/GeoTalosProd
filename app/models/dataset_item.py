import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DatasetItem(Base):
    """App-DB cache of individual STAC items produced during dataset ingestion.

    Each row mirrors one STAC item in pgSTAC.  ``stac_item_id`` is the
    canonical identifier; ``is_active = false`` marks items that have been
    superseded or whose source file was removed.
    """

    __tablename__ = "dataset_items"
    __table_args__ = (
        UniqueConstraint("stac_item_id", name="uq_dataset_items_stac_item_id"),
        Index("idx_dataset_items_dataset", "dataset_id"),
        Index("idx_dataset_items_org", "organization_id"),
        Index("idx_dataset_items_active", "dataset_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    stac_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stac_collection_id: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_uri: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # GeoJSON geometry stored as JSONB — avoids PostGIS dependency for simple
    # bbox/footprint lookups from the API. Spatial queries use pgSTAC directly.
    geometry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    item_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    properties_cache: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="items")
    organization: Mapped["Organization"] = relationship("Organization")
