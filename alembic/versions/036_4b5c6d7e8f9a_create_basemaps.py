"""Create basemaps table.

Revision ID: 4b5c6d7e8f9a
Revises: 3a4b5c6d7e8f
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "4b5c6d7e8f9a"
down_revision = "3a4b5c6d7e8f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "basemaps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(20), nullable=False),
        sa.Column("local_path", sa.String(500), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("config", JSONB, nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("file_type IN ('pmtiles', 'mbtiles')", name="ck_basemaps_file_type"),
    )
    op.create_index("idx_basemaps_org", "basemaps", ["organization_id"])

    op.execute("ALTER TABLE basemaps ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY basemaps_org_isolation ON basemaps
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
    """)


def downgrade():
    op.execute("DROP POLICY IF EXISTS basemaps_org_isolation ON basemaps")
    op.drop_index("idx_basemaps_org")
    op.drop_table("basemaps")
