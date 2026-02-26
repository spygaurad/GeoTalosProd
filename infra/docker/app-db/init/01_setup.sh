#!/usr/bin/env bash
# =============================================================================
# app-db initialisation — runs once on first container start
#
# Creates:
#   - PostGIS + uuid-ossp extensions on geoplat
#   - app_user   (API queries, subject to RLS)
#   - celery_worker (Celery tasks, BYPASSRLS)
#
# Passwords are injected via the compose environment:
#   APP_USER_PASSWORD      (default: app_pass)
#   CELERY_WORKER_PASSWORD (default: celery_pass)
# =============================================================================
set -euo pipefail

APP_PWD="${APP_USER_PASSWORD:-app_pass}"
CELERY_PWD="${CELERY_WORKER_PASSWORD:-celery_pass}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL

    ---------------------------------------------------------------------------
    -- Extensions
    ---------------------------------------------------------------------------
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS btree_gist;   -- needed for GIST indexes on range types

    ---------------------------------------------------------------------------
    -- app_user
    --   Used by FastAPI (asyncpg). Subject to Row-Level Security policies.
    --   NEVER grant BYPASSRLS to this role.
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            CREATE ROLE app_user WITH
                LOGIN
                PASSWORD '${APP_PWD}'
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                NOINHERIT
                CONNECTION LIMIT 50;
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE geoplat TO app_user;
    -- Table-level grants are applied by Alembic migrations after schema creation.

    ---------------------------------------------------------------------------
    -- celery_worker
    --   Used by Celery tasks (psycopg2 sync). BYPASSRLS so tasks can operate
    --   across all tenants without per-request RLS context.
    --   NEVER expose this role or its credentials to API-facing code paths.
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'celery_worker') THEN
            CREATE ROLE celery_worker WITH
                LOGIN
                PASSWORD '${CELERY_PWD}'
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                NOINHERIT
                BYPASSRLS
                CONNECTION LIMIT 20;
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE geoplat TO celery_worker;

SQL

echo "✓ app-db: extensions and roles created."
