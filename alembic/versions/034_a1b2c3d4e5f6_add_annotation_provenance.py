"""add annotation-level provenance (created_by_user_id + created_by_job_id)

Revision ID: a1b2c3d4e5f6
Revises: 09534c356d8e
Create Date: 2026-03-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f6"
down_revision = "09534c356d8e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add created_by_user_id — nullable FK to users
    op.add_column(
        "annotations",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_annotations_created_by_user",
        "annotations",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Add created_by_job_id — nullable FK to jobs
    op.add_column(
        "annotations",
        sa.Column("created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_annotations_created_by_job",
        "annotations",
        "jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Backfill from annotation_set's creator columns
    op.execute(
        """
        UPDATE annotations a
        SET created_by_user_id = s.created_by_user_id,
            created_by_job_id  = s.created_by_job_id
        FROM annotation_sets s
        WHERE a.annotation_set_id = s.id
          AND a.created_by_user_id IS NULL
          AND a.created_by_job_id IS NULL
        """
    )

    # XOR check constraint — exactly one creator must be set
    op.create_check_constraint(
        "ck_annotations_creator",
        "annotations",
        "(created_by_user_id IS NOT NULL AND created_by_job_id IS NULL) OR "
        "(created_by_user_id IS NULL AND created_by_job_id IS NOT NULL)",
    )

    # Indexes for provenance queries
    op.create_index("idx_annotations_created_by_user", "annotations", ["created_by_user_id"])
    op.create_index("idx_annotations_created_by_job", "annotations", ["created_by_job_id"])


def downgrade() -> None:
    op.drop_index("idx_annotations_created_by_job", table_name="annotations")
    op.drop_index("idx_annotations_created_by_user", table_name="annotations")
    op.drop_constraint("ck_annotations_creator", "annotations", type_="check")
    op.drop_constraint("fk_annotations_created_by_job", "annotations", type_="foreignkey")
    op.drop_column("annotations", "created_by_job_id")
    op.drop_constraint("fk_annotations_created_by_user", "annotations", type_="foreignkey")
    op.drop_column("annotations", "created_by_user_id")
