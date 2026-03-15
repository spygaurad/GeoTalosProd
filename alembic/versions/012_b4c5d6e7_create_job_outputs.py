"""create job outputs

Revision ID: b4c5d6e7
Revises: a3b4c5d6
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b4c5d6e7"
down_revision = "a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_outputs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("output_type", sa.String(length=50), nullable=False),
        sa.Column("output_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_job_outputs_job", "job_outputs", ["job_id"])
    op.create_index(
        "idx_job_outputs_type_id",
        "job_outputs",
        ["output_type", "output_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_job_outputs_type_id", table_name="job_outputs")
    op.drop_index("idx_job_outputs_job", table_name="job_outputs")
    op.drop_table("job_outputs")
