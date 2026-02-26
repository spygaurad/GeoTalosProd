#!/usr/bin/env bash
# =============================================================================
# stac-db initialisation — runs once on first container start
#
# The base image (ghcr.io/stac-utils/pgstac:v0.9.6) installs the pgSTAC
# schema via its own migration, which may run AFTER this script in some
# versions.  We therefore:
#   1. Create the roles unconditionally (safe — idempotent DO block).
#   2. Grant CONNECT on the database.
#   3. Grant schema-level permissions only if the pgstac schema already
#      exists; ALTER DEFAULT PRIVILEGES covers any tables added later.
#
# Creates:
#   - pgstac_ingest  (write access — used by ingestion Celery worker)
#   - pgstac_read    (read-only   — used by stac-api read pool + titiler)
#
# Passwords are injected via the compose environment:
#   STAC_INGEST_PASSWORD  (default: ingest_pass)
#   STAC_READ_PASSWORD    (default: read_pass)
# =============================================================================
set -euo pipefail

INGEST_PWD="${STAC_INGEST_PASSWORD:-ingest_pass}"
READ_PWD="${STAC_READ_PASSWORD:-read_pass}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL

    ---------------------------------------------------------------------------
    -- pgstac_ingest
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgstac_ingest') THEN
            CREATE ROLE pgstac_ingest WITH
                LOGIN
                PASSWORD '${INGEST_PWD}'
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                CONNECTION LIMIT 20;
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE pgstac TO pgstac_ingest;

    ---------------------------------------------------------------------------
    -- pgstac_read
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgstac_read') THEN
            CREATE ROLE pgstac_read WITH
                LOGIN
                PASSWORD '${READ_PWD}'
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                CONNECTION LIMIT 30;
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE pgstac TO pgstac_read;

    ---------------------------------------------------------------------------
    -- Schema-level grants — only if pgstac schema already exists.
    -- ALTER DEFAULT PRIVILEGES ensures future tables are also covered.
    -- The stac-api and titiler connect as superuser (postgres) at startup
    -- and re-run migrations, which will apply any missing grants at that point.
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'pgstac') THEN
            GRANT USAGE ON SCHEMA pgstac TO pgstac_ingest;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pgstac TO pgstac_ingest;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA pgstac TO pgstac_ingest;
            ALTER DEFAULT PRIVILEGES IN SCHEMA pgstac
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgstac_ingest;

            GRANT USAGE ON SCHEMA pgstac TO pgstac_read;
            GRANT SELECT ON ALL TABLES IN SCHEMA pgstac TO pgstac_read;
            ALTER DEFAULT PRIVILEGES IN SCHEMA pgstac
                GRANT SELECT ON TABLES TO pgstac_read;

            RAISE NOTICE 'pgstac schema found — grants applied.';
        ELSE
            -- Schema will be created by pgSTAC migration after this script.
            -- ALTER DEFAULT PRIVILEGES still applies to future tables.
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgstac_ingest;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
                GRANT SELECT ON TABLES TO pgstac_read;

            RAISE NOTICE 'pgstac schema not yet present — deferred grants set via ALTER DEFAULT PRIVILEGES.';
        END IF;
    END
    \$\$;

SQL

echo "✓ stac-db: pgstac_ingest and pgstac_read roles created."
