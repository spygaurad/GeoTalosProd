"""pg17 uuid_v7 alias

On PostgreSQL 17 the native gen_uuid_v7() function is available.
This migration replaces the custom plpgsql uuid_generate_v7() that was
created in migration 014 with a thin SQL wrapper that calls the PG17
built-in.  The function signature is preserved so that all existing
server_default values (annotations.id etc.) continue to work unchanged.

On PG < 17 gen_uuid_v7() does not exist, so we fall back to recreating
the original plpgsql implementation.  The DO block detects the PG version
and picks the right definition automatically.

Revision ID: aa1b2c3d
Revises: fb1a2c3d
Create Date: 2026-03-15 00:00:00.000000
"""

from alembic import op

revision = "aa1b2c3d"
down_revision = "fb1a2c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # On PG17+ delegate to the native gen_uuid_v7().
    # On PG15/16 keep the original plpgsql implementation as a fallback.
    op.execute(
        """
        DO $$
        BEGIN
          IF current_setting('server_version_num')::int >= 170000 THEN
            -- PG17: native gen_uuid_v7() is available; make our function a
            -- thin alias so existing server_default calls keep working.
            CREATE OR REPLACE FUNCTION uuid_generate_v7()
              RETURNS uuid
              LANGUAGE sql
              VOLATILE
            AS $fn$ SELECT gen_uuid_v7() $fn$;
          ELSE
            -- PG < 17: recreate the original plpgsql implementation.
            CREATE OR REPLACE FUNCTION uuid_generate_v7()
              RETURNS uuid AS $fn$
              DECLARE
                unix_ts_ms bytea;
                uuid_bytes bytea;
              BEGIN
                unix_ts_ms := decode(
                  lpad(to_hex(floor(extract(epoch from clock_timestamp()) * 1000)::bigint), 12, '0'),
                  'hex'
                );
                uuid_bytes := unix_ts_ms || gen_random_bytes(10);
                uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
                uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
                RETURN encode(uuid_bytes, 'hex')::uuid;
              END;
              $fn$ LANGUAGE plpgsql VOLATILE;
          END IF;
        END;
        $$;
        """
    )


def downgrade() -> None:
    # Restore the original plpgsql implementation unconditionally so the
    # schema works on PG15/16 again after a downgrade.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION uuid_generate_v7()
          RETURNS uuid AS $$
          DECLARE
            unix_ts_ms bytea;
            uuid_bytes bytea;
          BEGIN
            unix_ts_ms := decode(
              lpad(to_hex(floor(extract(epoch from clock_timestamp()) * 1000)::bigint), 12, '0'),
              'hex'
            );
            uuid_bytes := unix_ts_ms || gen_random_bytes(10);
            uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
            uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
            RETURN encode(uuid_bytes, 'hex')::uuid;
          END;
          $$ LANGUAGE plpgsql VOLATILE;
        """
    )
