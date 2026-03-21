import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class FeatureLayer(Base):
    __tablename__ = "feature_layers"
    __table_args__ = (Index("idx_feature_layers_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    layer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[bytes] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=False
    )
    properties: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped["Organization"] = relationship("Organization")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
