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
MARTIN_READER_PWD="${MARTIN_READER_PASSWORD:-martin_pass}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL

    ---------------------------------------------------------------------------
    -- Extensions
    ---------------------------------------------------------------------------
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS btree_gist;   -- needed for GIST indexes on range types

    DO \$\$
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
        RAISE NOTICE 'pg_stat_statements created successfully.'; -- pg_stat_statements - attempt creation, ignore failure if not preloaded
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'Could not create pg_stat_statements (library not preloaded?). Proceeding without it.';
    END;
    \$\$;
    
    CREATE EXTENSION IF NOT EXISTS pgcrypto;      -- for gen_random_bytes (used by extensions)
    CREATE EXTENSION IF NOT EXISTS ltree;         -- for hierarchical paths in annotation_classes

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
    -- Alembic needs CREATE on schema public to create alembic_version + first tables.
    GRANT USAGE, CREATE ON SCHEMA public TO app_user;
    -- Ensure app_user can access tables/sequences created by migrations.
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
    -- Apply grants to future tables/sequences as migrations evolve.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO app_user;

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
    -- Worker role may not create tables, but should access schema objects.
    GRANT USAGE ON SCHEMA public TO celery_worker;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO celery_worker;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO celery_worker;
    ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO celery_worker;
    ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO celery_worker;

    ---------------------------------------------------------------------------
    -- martin_reader
    --   Read-only role for the Martin vector tile server.
    --   BYPASSRLS is required so Martin can read rows regardless of the RLS
    --   policies (Martin does not set per-request session variables).
    --   SELECT-only: Martin cannot mutate any data.
    --   NEVER grant INSERT/UPDATE/DELETE to this role.
    ---------------------------------------------------------------------------
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'martin_reader') THEN
            CREATE ROLE martin_reader WITH
                LOGIN
                PASSWORD '${MARTIN_READER_PWD}'
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                NOINHERIT
                BYPASSRLS
                CONNECTION LIMIT 10;
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE geoplat TO martin_reader;
    GRANT USAGE ON SCHEMA public TO martin_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO martin_reader;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO martin_reader;
    -- Cover tables created by app_user (Alembic migrations)
    ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public
        GRANT SELECT ON TABLES TO martin_reader;
    ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO martin_reader;
    -- Cover tables created by the postgres superuser (extensions, etc.)
    ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
        GRANT SELECT ON TABLES TO martin_reader;

SQL

echo "✓ app-db:  extensions and roles created (app_user, celery_worker, martin_reader)."
