"""Add tile_tier to embeddings — scale-aware similarity search.

``search()`` compared candidates purely on cosine distance, with no regard
for the real-world size of the selection each embedding was cropped from —
two objects of very different real-world scale could rank as "similar"
purely because both were resized to the same crop_size_px pixel grid.
``tile_tier`` snaps every embedding's source geometry to one of a small set
of log-spaced scale tiers (see geoops/scale.py), and search() now requires
candidates to share the reference's tier.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embeddings",
        sa.Column("tile_tier", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "idx_embeddings_org_tier", "embeddings", ["organization_id", "model_name", "tile_tier"]
    )


def downgrade() -> None:
    op.drop_index("idx_embeddings_org_tier", table_name="embeddings")
    op.drop_column("embeddings", "tile_tier")
