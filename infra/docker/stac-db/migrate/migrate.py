#!/usr/bin/env python3
"""
Run pgSTAC migrations and apply grants to application roles.
This script is intended to be run once after the stac-db container is healthy.
"""

import os
import sys
from pypgstac.db import PgstacDB
from pypgstac.migrate import Migrate
import psycopg2

def main():
    # Get superuser password from environment
    superuser_pass = os.environ.get("STAC_DB_SUPERUSER_PASSWORD", "pgstac_admin")
    dsn = f"postgresql://postgres:{superuser_pass}@stac-db:5432/pgstac"

    print("Running pgSTAC migration...")
    db = PgstacDB(dsn=dsn)
    m = Migrate(db)
    m.run_migration()
    print("Migration completed. Applying grants...")

    # Apply grants as superuser
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("GRANT USAGE ON SCHEMA pgstac TO pgstac_ingest;")
    cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pgstac TO pgstac_ingest;")
    cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA pgstac TO pgstac_ingest;")
    cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA pgstac GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pgstac_ingest;")
    cur.execute("GRANT USAGE ON SCHEMA pgstac TO pgstac_read;")
    cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA pgstac TO pgstac_read;")
    # Cover tables created by postgres superuser
    cur.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA pgstac GRANT SELECT ON TABLES TO pgstac_read;")
    # Cover per-collection partition tables created by pgstac_ingest (the root cause of titiler 404s)
    cur.execute("ALTER DEFAULT PRIVILEGES FOR ROLE pgstac_ingest IN SCHEMA pgstac GRANT SELECT ON TABLES TO pgstac_read;")
    cur.execute("ALTER DEFAULT PRIVILEGES FOR ROLE pgstac_ingest IN SCHEMA pgstac GRANT USAGE, SELECT ON SEQUENCES TO pgstac_read;")
    conn.commit()
    cur.close()
    conn.close()
    print("Grants applied successfully.")
    sys.exit(0)

if __name__ == "__main__":
    main()