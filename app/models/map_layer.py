import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MapLayer(Base):
    __tablename__ = "map_layers"
    __table_args__ = (
        Index("idx_map_layers_map", "map_id"),
        Index("idx_map_layers_dataset", "dataset_id"),
        Index("idx_map_layers_style", "style_id"),
        CheckConstraint(
            "(source_type = 'dataset' AND dataset_id IS NOT NULL AND stac_item_id IS NULL AND tile_service_url IS NULL) OR "
            "(source_type = 'stac_item' AND stac_item_id IS NOT NULL AND dataset_id IS NULL AND tile_service_url IS NULL) OR "
            "(source_type = 'tile_service' AND tile_service_url IS NOT NULL AND dataset_id IS NULL AND stac_item_id IS NULL)",
            name="ck_map_layers_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maps.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    layer_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=True
    )
    stac_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tile_service_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("styles.id", ondelete="SET NULL"), nullable=True
    )
    style_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    time_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    opacity: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    min_zoom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_zoom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    map: Mapped["Map"] = relationship("Map", back_populates="layers")
    dataset: Mapped["Dataset | None"] = relationship("Dataset", back_populates="map_layers")
    style: Mapped["Style | None"] = relationship("Style")
