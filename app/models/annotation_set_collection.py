import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AnnotationSetCollection(Base):
    __tablename__ = "annotation_set_collections"
    __table_args__ = (
        Index("idx_annotation_set_collections_org", "organization_id"),
        Index("idx_annotation_set_collections_schema", "schema_id"),
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_annotation_set_collections_org_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_schemas.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    organization: Mapped["Organization"] = relationship("Organization")
    schema: Mapped["AnnotationSchema"] = relationship("AnnotationSchema")
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    annotation_set_links: Mapped[list["AnnotationSetCollectionItem"]] = relationship(
        "AnnotationSetCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
    )
