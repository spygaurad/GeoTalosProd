"""Add embeddings.created_by_job_id + annotation/model cache constraint.

Anomaly detection (a batch job) embeds real annotations into the curated
``embeddings`` bank rather than the grid-tile cache — these are genuine
annotation-tied selections, just created in bulk instead of one at a time.
``created_by_job_id`` gives them the same provenance ``annotation_versions``-
style tracking already used elsewhere (e.g. ``Annotation.created_by_job_id``).
The partial unique index on (annotation_id, model_id) is the cache key both
the anomaly job and the manual create endpoint check before embedding an
annotation again — one annotation only ever needs one embedding per model.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embeddings",
        sa.Column("created_by_job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(
        "uq_embeddings_annotation_model", "embeddings", ["annotation_id", "model_id"],
        unique=True, postgresql_where=sa.text("annotation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_embeddings_annotation_model", table_name="embeddings")
    op.drop_column("embeddings", "created_by_job_id")
