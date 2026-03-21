"""Create tile_sources table.

Revision ID: 5c6d7e8f9a0b
Revises: 4b5c6d7e8f9a
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "5c6d7e8f9a0b"
down_revision = "4b5c6d7e8f9a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tile_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("tile_service_url", sa.String(500), nullable=True),
        sa.Column("config", JSONB, nullable=True),
        sa.Column("dataset_item_id", UUID(as_uuid=True), sa.ForeignKey("dataset_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("basemap_id", UUID(as_uuid=True), sa.ForeignKey("basemaps.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source_type IN ('raster', 'vector', 'basemap')", name="ck_tile_sources_type"),
    )
    op.create_index("idx_tile_sources_org", "tile_sources", ["organization_id"])

    # RLS
    op.execute("ALTER TABLE tile_sources ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tile_sources_org_isolation ON tile_sources
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
    """)


def downgrade():
    op.execute("DROP POLICY IF EXISTS tile_sources_org_isolation ON tile_sources")
    op.drop_index("idx_tile_sources_org")
    op.drop_table("tile_sources")
