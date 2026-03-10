import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PendingInvitation(Base):
    """
    Temporary store for Clerk invitation metadata.

    Written by the `organizationInvitation.accepted` webhook event.
    Consumed (and deleted) by the `organizationMembership.created` event to apply
    custom app_role and project access set at invite time.

    No RLS — written by the webhook handler which has no user/org context.
    Records are cleaned up immediately after the membership is created.
    """

    __tablename__ = "pending_invitations"
    __table_args__ = (
        UniqueConstraint("clerk_org_id", "email", name="uq_pending_inv_org_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clerk_org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Role to assign on membership creation — overrides the Clerk role if present.
    app_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Project IDs to grant access to on membership creation.
    project_ids: Mapped[list] = mapped_column(ARRAY(Text()), nullable=False, server_default="{}")
    # clerk_user_id of the admin who sent the invite.
    invited_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
