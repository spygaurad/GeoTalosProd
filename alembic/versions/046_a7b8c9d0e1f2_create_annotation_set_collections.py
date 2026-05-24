"""Create annotation_set_collections tables.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotation_set_collections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "schema_id",
            UUID(as_uuid=True),
            sa.ForeignKey("annotation_schemas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "name", name="uq_annotation_set_collections_org_name"),
    )
    op.create_index("idx_annotation_set_collections_org", "annotation_set_collections", ["organization_id"])
    op.create_index("idx_annotation_set_collections_schema", "annotation_set_collections", ["schema_id"])

    op.create_table(
        "annotation_set_collection_items",
        sa.Column("collection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_set_id", UUID(as_uuid=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("linked_by", UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["collection_id"], ["annotation_set_collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["annotation_set_id"], ["annotation_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("collection_id", "annotation_set_id"),
    )
    op.create_index(
        "idx_annotation_set_collection_items_set",
        "annotation_set_collection_items",
        ["annotation_set_id"],
    )

    op.execute("ALTER TABLE annotation_set_collections ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY annotation_set_collections_org_isolation ON annotation_set_collections
        USING (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """
    )

    op.execute("ALTER TABLE annotation_set_collection_items ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY annotation_set_collection_items_org_isolation ON annotation_set_collection_items
        USING (
            collection_id IN (
                SELECT id
                FROM annotation_set_collections
                WHERE organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS annotation_set_collection_items_org_isolation ON annotation_set_collection_items")
    op.execute("ALTER TABLE annotation_set_collection_items DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS annotation_set_collections_org_isolation ON annotation_set_collections")
    op.execute("ALTER TABLE annotation_set_collections DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_annotation_set_collection_items_set", table_name="annotation_set_collection_items")
    op.drop_table("annotation_set_collection_items")
    op.drop_index("idx_annotation_set_collections_schema", table_name="annotation_set_collections")
    op.drop_index("idx_annotation_set_collections_org", table_name="annotation_set_collections")
    op.drop_table("annotation_set_collections")
