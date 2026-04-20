import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("idx_projects_org", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
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
    default_annotation_schema_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_schemas.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped["Organization"] = relationship("Organization", back_populates="projects")
    default_annotation_schema: Mapped["AnnotationSchema | None"] = relationship(
        "AnnotationSchema", foreign_keys=[default_annotation_schema_id]
    )
    maps: Mapped[list["Map"]] = relationship(
        "Map",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    annotation_set_links: Mapped[list["ProjectAnnotationSet"]] = relationship(
        "ProjectAnnotationSet",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    dataset_links: Mapped[list["ProjectDataset"]] = relationship(
        "ProjectDataset",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
