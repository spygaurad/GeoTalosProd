import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('org:admin','org:member','org:viewer')",
            name="chk_org_memberships_role",
        ),
        # Added to represent invitation/member lifecycle state explicitly.
        CheckConstraint(
            "status IN ('active','invited','suspended')",
            name="chk_org_memberships_status",
        ),
        Index("idx_org_memberships_org", "organization_id"),
        Index("idx_org_memberships_user", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="org:viewer")
    # Added to track who initiated the org invitation/membership assignment.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Added to support invite -> active -> suspended transitions cleanly.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    # Added for audit trail of membership creation timing.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Added to track role/status lifecycle changes over time.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
