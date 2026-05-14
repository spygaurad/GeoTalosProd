"""Create map_aois table.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "map_aois",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("map_id", UUID(as_uuid=True), sa.ForeignKey("maps.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("bbox_4326", JSONB, nullable=False),
        sa.Column("geometry", JSONB, nullable=True),
        sa.Column("selection_config", JSONB, nullable=True),
        sa.Column("render_config", JSONB, nullable=True),
        sa.Column("temporal_config", JSONB, nullable=True),
        sa.Column("analysis_config", JSONB, nullable=True),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("opacity", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_map_aois_map", "map_aois", ["map_id"])
    op.create_index("idx_map_aois_org", "map_aois", ["organization_id"])
    op.create_index("idx_map_aois_visible", "map_aois", ["map_id", "visible"])

    op.execute("ALTER TABLE map_aois ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY map_aois_org_isolation ON map_aois
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS map_aois_org_isolation ON map_aois")
    op.drop_index("idx_map_aois_visible", table_name="map_aois")
    op.drop_index("idx_map_aois_org", table_name="map_aois")
    op.drop_index("idx_map_aois_map", table_name="map_aois")
    op.drop_table("map_aois")
