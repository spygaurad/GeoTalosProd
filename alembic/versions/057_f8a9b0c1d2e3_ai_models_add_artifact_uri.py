"""Add ai_models.artifact_uri for YOLO head-fine-tuning outputs.

Stores the S3 URI of a trained artifact for models produced by the
``finetune_model`` job (a head-only state_dict checkpoint, not a full model
weights file — it's meant to be overlaid onto a shared backbone by
yolo-service). Nullable: unrelated to HTTP-endpoint inference models, which
never populate this column.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_models", sa.Column("artifact_uri", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_models", "artifact_uri")
