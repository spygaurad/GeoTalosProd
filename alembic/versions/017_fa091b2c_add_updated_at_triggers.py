"""add updated_at triggers

Revision ID: fa091b2c
Revises: f8091a2b
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op

revision = "fa091b2c"
down_revision = "f8091a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE TRIGGER trigger_update_users
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_organizations
        BEFORE UPDATE ON organizations
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_projects
        BEFORE UPDATE ON projects
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_maps
        BEFORE UPDATE ON maps
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_map_layers
        BEFORE UPDATE ON map_layers
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_datasets
        BEFORE UPDATE ON datasets
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_styles
        BEFORE UPDATE ON styles
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_annotation_schemas
        BEFORE UPDATE ON annotation_schemas
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_annotation_classes
        BEFORE UPDATE ON annotation_classes
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_annotation_sets
        BEFORE UPDATE ON annotation_sets
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_annotations
        BEFORE UPDATE ON annotations
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_ai_models
        BEFORE UPDATE ON ai_models
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_jobs
        BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trigger_update_jobs ON jobs")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_ai_models ON ai_models")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_annotations ON annotations")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_annotation_sets ON annotation_sets")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_annotation_classes ON annotation_classes")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_annotation_schemas ON annotation_schemas")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_styles ON styles")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_datasets ON datasets")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_map_layers ON map_layers")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_maps ON maps")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_projects ON projects")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_organizations ON organizations")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_users ON users")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column")
