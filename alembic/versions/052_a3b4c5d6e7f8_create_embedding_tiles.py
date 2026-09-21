"""Create embedding_tiles — cache of grid-aligned AOI-scan patches.

Deliberately separate from ``embeddings`` (the curated bank behind
``search()``): every row here is auto-generated coverage from an AOI scan,
not something a user or annotation chose, and there can be far more of them.
The unique constraint on (dataset_item_id, model_id, tile_tier, tile_col,
tile_row) is the cache key that lets a re-scan of an overlapping AOI reuse
tiles a prior scan already embedded instead of redoing the work.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "embedding_tiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column(
            "dataset_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dataset_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tile_tier", sa.Integer(), nullable=False),
        sa.Column("tile_col", sa.Integer(), nullable=False),
        sa.Column("tile_row", sa.Integer(), nullable=False),
        sa.Column("bbox", JSONB, nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_by_job_id",
            UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "dataset_item_id", "model_id", "tile_tier", "tile_col", "tile_row",
            name="uq_embedding_tiles_grid",
        ),
    )
    op.create_index("idx_embedding_tiles_org_model", "embedding_tiles", ["organization_id", "model_name"])
    op.create_index("idx_embedding_tiles_dataset_item", "embedding_tiles", ["dataset_item_id"])

    op.execute("ALTER TABLE embedding_tiles ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY embedding_tiles_org_isolation ON embedding_tiles
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS embedding_tiles_org_isolation ON embedding_tiles")
    op.drop_index("idx_embedding_tiles_dataset_item", table_name="embedding_tiles")
    op.drop_index("idx_embedding_tiles_org_model", table_name="embedding_tiles")
    op.drop_table("embedding_tiles")
