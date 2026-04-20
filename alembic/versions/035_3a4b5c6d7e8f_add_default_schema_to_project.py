"""Add default_annotation_schema_id to projects.

Revision ID: 3a4b5c6d7e8f
Revises: 91a2b3c4
Create Date: 2026-03-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "3a4b5c6d7e8f"
down_revision = "91a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column("default_annotation_schema_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_default_annotation_schema",
        "projects",
        "annotation_schemas",
        ["default_annotation_schema_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_projects_default_annotation_schema", "projects", type_="foreignkey")
    op.drop_column("projects", "default_annotation_schema_id")
