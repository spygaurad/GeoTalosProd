import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clerk_user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # These foreign_keys selectors avoid ambiguity because membership tables also
    # have invited_by / added_by references that point to this same users table.
    org_memberships: Mapped[list["OrgMembership"]] = relationship(
        "OrgMembership",
        back_populates="user",
        foreign_keys="OrgMembership.user_id",
        cascade="all, delete-orphan",
    )
    invited_org_memberships: Mapped[list["OrgMembership"]] = relationship(
        "OrgMembership",
        foreign_keys="OrgMembership.invited_by",
    )

    project_memberships: Mapped[list["ProjectMember"]] = relationship(
        "ProjectMember",
        back_populates="user",
        foreign_keys="ProjectMember.user_id",
        cascade="all, delete-orphan",
    )
    added_project_memberships: Mapped[list["ProjectMember"]] = relationship(
        "ProjectMember",
        foreign_keys="ProjectMember.added_by",
    )

    created_projects: Mapped[list["Project"]] = relationship(
        "Project",
        foreign_keys="Project.created_by",
    )
    archived_projects: Mapped[list["Project"]] = relationship(
        "Project",
        foreign_keys="Project.archived_by",
    )
