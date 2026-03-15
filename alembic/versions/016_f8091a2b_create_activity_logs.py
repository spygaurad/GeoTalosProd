"""create activity logs

Revision ID: f8091a2b
Revises: e7f8091a
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f8091a2b"
down_revision = "e7f8091a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("changes", postgresql.JSONB(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_activity_logs_org", "activity_logs", ["organization_id"])
    op.create_index("idx_activity_logs_user", "activity_logs", ["user_id"])
    op.create_index(
        "idx_activity_logs_entity",
        "activity_logs",
        ["entity_type", "entity_id"],
    )
    op.create_index("idx_activity_logs_created_at", "activity_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_activity_logs_created_at", table_name="activity_logs")
    op.drop_index("idx_activity_logs_entity", table_name="activity_logs")
    op.drop_index("idx_activity_logs_user", table_name="activity_logs")
    op.drop_index("idx_activity_logs_org", table_name="activity_logs")
    op.drop_table("activity_logs")
