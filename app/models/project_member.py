import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin','editor','annotator','viewer')",
            name="chk_project_members_role",
        ),
        # Added so membership can be deactivated without deleting history.
        CheckConstraint(
            "status IN ('active','removed')",
            name="chk_project_members_status",
        ),
        Index("idx_project_members_project", "project_id"),
        Index("idx_project_members_user", "user_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="viewer")
    # Added to capture who granted project access.
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Added for membership lifecycle (active/removed) without hard deletes.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    # Added for timeline/audit of project access grants.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Added to track membership role/status changes over time.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
