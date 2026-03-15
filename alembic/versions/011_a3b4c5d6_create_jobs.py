"""create jobs

Revision ID: a3b4c5d6
Revises: 92a3b4c5
Create Date: 2026-03-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a3b4c5d6"
down_revision = "92a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("input_refs", postgresql.JSONB(), nullable=True),
        sa.Column("logs", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "model_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_jobs_org", "jobs", ["organization_id"])
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_model", "jobs", ["model_id"])
    op.create_index(
        "idx_jobs_input_refs",
        "jobs",
        ["input_refs"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_input_refs", table_name="jobs")
    op.drop_index("idx_jobs_model", table_name="jobs")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_index("idx_jobs_org", table_name="jobs")
    op.drop_table("jobs")
