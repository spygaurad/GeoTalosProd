"""create project_members table

Revision ID: e50f0c05
Revises: d40f0c04
Create Date: 2026-03-04 00:04:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e50f0c05"
down_revision: Union[str, None] = "d40f0c04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=50), server_default="viewer", nullable=False),
        sa.Column("added_by", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('admin','editor','annotator','viewer')", name="chk_project_members_role"),
        sa.CheckConstraint("status IN ('active','removed')", name="chk_project_members_status"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index("idx_project_members_project", "project_members", ["project_id"], unique=False)
    op.create_index("idx_project_members_user", "project_members", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_project_members_user", table_name="project_members")
    op.drop_index("idx_project_members_project", table_name="project_members")
    op.drop_table("project_members")
