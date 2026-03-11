"""create models table

Revision ID: j00f0c10
Revises: i90f0c09
Create Date: 2026-03-09 08:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "j00f0c10"
down_revision: Union[str, None] = "i90f0c09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=True),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_models_org", "models", ["organization_id"], unique=False)
    op.create_index("idx_models_org_name", "models", ["organization_id", "name"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_models_org_name", table_name="models")
    op.drop_index("idx_models_org", table_name="models")
    op.drop_table("models")
