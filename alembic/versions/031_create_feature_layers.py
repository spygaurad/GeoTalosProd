"""Create feature_layers table.

Revision ID: 6d7e8f9a0b1c
Revises: 5c6d7e8f9a0b
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "6d7e8f9a0b1c"
down_revision = "5c6d7e8f9a0b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feature_layers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("layer_name", sa.String(255), nullable=False),
        sa.Column("geometry", Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=False),
        sa.Column("properties", JSONB, nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_feature_layers_org", "feature_layers", ["organization_id"])
    op.execute("CREATE INDEX idx_feature_layers_geom ON feature_layers USING GIST (geometry)")

    # RLS
    op.execute("ALTER TABLE feature_layers ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY feature_layers_org_isolation ON feature_layers
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
    """)


def downgrade():
    op.execute("DROP POLICY IF EXISTS feature_layers_org_isolation ON feature_layers")
    op.execute("DROP INDEX IF EXISTS idx_feature_layers_geom")
    op.drop_index("idx_feature_layers_org")
    op.drop_table("feature_layers")
