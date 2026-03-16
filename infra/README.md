# AwakeForest Infrastructure

## PostgreSQL Major-Version Data Migration

When upgrading across a PostgreSQL major version (e.g., PG 15 → PG 17), the
data directory format changes.  `pg_upgrade` is not practical inside Docker,
so the procedure is: dump → delete volumes → start new containers → restore.

> **Warning:** This destroys the Docker volumes.  Run on a machine with enough
> disk space to hold two copies of the database.  Back up to a safe location
> before proceeding.

### 1. Dump existing data

```bash
# Application DB
docker exec awakeforest-app-db \
  pg_dump -U postgres -Fc geoplat > geoplat_backup.dump

# STAC catalog DB
docker exec awakeforest-stac-db \
  pg_dump -U postgres -Fc pgstac > pgstac_backup.dump
```

### 2. Stop services and remove volumes

```bash
docker compose down -v
```

This deletes `awakeforest-app-db-data`, `awakeforest-stac-db-data`, and all
other named volumes.  The dump files created in step 1 are on the host and are
not affected.

### 3. Start the new database containers only

```bash
docker compose up -d app-db stac-db
# Wait for healthchecks to pass before restoring
docker compose ps
```

### 4. Restore

```bash
# Application DB — restore into the freshly initialised geoplat database
docker exec -i awakeforest-app-db \
  pg_restore -U postgres -d geoplat --no-owner --role=postgres \
  < geoplat_backup.dump

# STAC catalog DB
docker exec -i awakeforest-stac-db \
  pg_restore -U postgres -d pgstac --no-owner --role=postgres \
  < pgstac_backup.dump
```

### 5. Re-run Alembic migrations

Any migrations added since the dump was taken (e.g., migration 019 for the
PG17 `gen_uuid_v7()` alias) must be applied:

```bash
docker compose run --rm api alembic upgrade head
```

### 6. Start remaining services

```bash
docker compose up -d
```

---

## Environment Setup

Copy `.env.docker` to `.env` and fill in all values marked `***` before
running `docker compose up`:

```bash
cp .env.docker .env
# Edit .env — change every placeholder marked ***
```

Secrets that **must** be changed before any non-local deployment:
- `APP_DB_SUPERUSER_PASSWORD`
- `APP_USER_PASSWORD`
- `CELERY_WORKER_PASSWORD`
- `MARTIN_READER_PASSWORD`
- `STAC_DB_SUPERUSER_PASSWORD`
- `STAC_INGEST_PASSWORD`
- `STAC_READ_PASSWORD`
- `REDIS_PASSWORD`
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`
- `FLOWER_PASSWORD`
- `CLERK_SECRET_KEY`
- `CLERK_WEBHOOK_SECRET`
- `INTERNAL_API_KEY`
- `API_KEY_SALT`

Generate random secrets:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Service Ports (defaults from `.env.docker`)

| Service        | Host Port |
|----------------|-----------|
| FastAPI (api)  | 2024      |
| PostgreSQL app | 5432      |
| PostgreSQL stac| 5433      |
| Redis          | 6379      |
| MinIO API (S3) | 9002      |
| MinIO Console  | 9003      |
| STAC API       | 8081      |
| titiler        | 8082      |
| Martin (MVT)   | 3000      |
| Flower (Celery)| 5555      |
