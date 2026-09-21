"""Add ai_models.backbone_model_id (self-referential).

Supports the shared-backbone + per-class-head architecture: a "backbone"
ai_models row (e.g. DINOv2, config={"source": "huggingface", "model_id":
"facebook/dinov2-base"}) has no artifact_uri of its own — it's a standard
pretrained checkpoint the serving service caches. A "head" ai_models row
(type='detect_head'/'segment_head') points at its shared backbone via this
column, and carries its own artifact_uri (S3 path to that head's state_dict
only) and a single model_class_mappings row binding it to one annotation
class. Nullable: irrelevant to plain HTTP-endpoint inference models.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("backbone_model_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_models_backbone_model",
        "ai_models",
        "ai_models",
        ["backbone_model_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_ai_models_backbone_model", "ai_models", ["backbone_model_id"])


def downgrade() -> None:
    op.drop_index("idx_ai_models_backbone_model", table_name="ai_models")
    op.drop_constraint("fk_ai_models_backbone_model", "ai_models", type_="foreignkey")
    op.drop_column("ai_models", "backbone_model_id")
