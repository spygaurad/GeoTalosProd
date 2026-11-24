"""Per organization style overrides for the public reference layers.

Reference layers are global and read only, but how they look is a per organization
choice (colour, opacity, boxes or heatmap). Only the fields a user changed are stored;
the UI fills in its defaults for the rest. Same per-org isolation policy as dino_datasets.
Uploaded layers keep their style here too, under "upload:<layer name>".

Revision ID: d1f4a8c2b7e5
Revises: 9b4e2c7a1f3d
Create Date: 2026-11-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "d1f4a8c2b7e5"
down_revision = "9b4e2c7a1f3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_layer_styles",
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("layer", sa.String(160), nullable=False),
        sa.Column("style", JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("organization_id", "layer"),
    )
    op.execute("ALTER TABLE reference_layer_styles ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY reference_layer_styles_org_isolation ON reference_layer_styles
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS reference_layer_styles_org_isolation ON reference_layer_styles")
    op.drop_table("reference_layer_styles")
