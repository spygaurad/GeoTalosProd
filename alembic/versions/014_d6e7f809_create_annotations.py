"""create annotations

Revision ID: d6e7f809
Revises: c5d6e7f8
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision = "d6e7f809"
down_revision = "c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_generate_v7()
        RETURNS uuid AS $$
        DECLARE
          unix_ts_ms bytea;
          uuid_bytes bytea;
        BEGIN
          unix_ts_ms := decode(lpad(to_hex(floor(extract(epoch from clock_timestamp()) * 1000)::bigint), 12, '0'), 'hex');
          uuid_bytes := unix_ts_ms || gen_random_bytes(10);
          uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
          uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
          RETURN encode(uuid_bytes, 'hex')::uuid;
        END;
        $$ LANGUAGE plpgsql VOLATILE;
        """
    )
    op.create_table(
        "annotations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "annotation_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("annotation_classes.id"),
            nullable=False,
        ),
        sa.Column("geometry", Geometry("Geometry", srid=4326, spatial_index=False), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_annotations_set", "annotations", ["annotation_set_id"])
    op.create_index("idx_annotations_class", "annotations", ["class_id"])
    op.create_index(
        "idx_annotations_confidence",
        "annotations",
        ["confidence"],
        postgresql_where=sa.text("confidence IS NOT NULL"),
    )
    op.create_index(
        "idx_annotations_geometry",
        "annotations",
        ["geometry"],
        postgresql_using="gist",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_annotation_geometry_type()
        RETURNS TRIGGER AS $$
        DECLARE
          allowed_types TEXT[];
          geom_type TEXT;
        BEGIN
          SELECT s.geometry_types INTO allowed_types
          FROM annotation_classes c
          JOIN annotation_schemas s ON c.schema_id = s.id
          WHERE c.id = NEW.class_id;

          geom_type := ST_GeometryType(NEW.geometry);

          IF NOT (
            geom_type = ANY(allowed_types) OR
            (geom_type = 'ST_Polygon' AND 'Polygon' = ANY(allowed_types)) OR
            (geom_type = 'ST_MultiPolygon' AND 'Polygon' = ANY(allowed_types)) OR
            (geom_type = 'ST_Point' AND 'Point' = ANY(allowed_types))
          ) THEN
            RAISE EXCEPTION 'Geometry type % not allowed for class %', geom_type, NEW.class_id;
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_annotation_geometry_type
        BEFORE INSERT OR UPDATE ON annotations
        FOR EACH ROW EXECUTE FUNCTION check_annotation_geometry_type();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS enforce_annotation_geometry_type ON annotations")
    op.execute("DROP FUNCTION IF EXISTS check_annotation_geometry_type")
    op.drop_index("idx_annotations_geometry", table_name="annotations")
    op.drop_index("idx_annotations_confidence", table_name="annotations")
    op.drop_index("idx_annotations_class", table_name="annotations")
    op.drop_index("idx_annotations_set", table_name="annotations")
    op.drop_table("annotations")
