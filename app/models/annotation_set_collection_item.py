import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AnnotationSetCollectionItem(Base):
    __tablename__ = "annotation_set_collection_items"
    __table_args__ = (
        Index("idx_annotation_set_collection_items_set", "annotation_set_id"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_set_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    annotation_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_sets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    linked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    collection: Mapped["AnnotationSetCollection"] = relationship(
        "AnnotationSetCollection",
        back_populates="annotation_set_links",
    )
    annotation_set: Mapped["AnnotationSet"] = relationship("AnnotationSet")
    linker: Mapped["User | None"] = relationship("User", foreign_keys=[linked_by])
