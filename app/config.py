from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    APP_DATABASE_URL: str  # required — no default to prevent silent weak-credential connections
    LOG_LEVEL: str = "INFO"
    # Keep explicit list for readable local-dev defaults. Tighten in deployed environments., read from env
    BACKEND_CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    ENVIRONMENT: str = ""

    # ── Clerk ─────────────────────────────────────────────────────────────────
    # "Frontend API" domain from Clerk Dashboard → API Keys
    # e.g. "happy-fox-42.clerk.accounts.dev"
    CLERK_FRONTEND_API: str = ""
    CLERK_SECRET_KEY: str = ""
    CLERK_WEBHOOK_SECRET: str = ""

    # Shared secret between Next.js webhook relay and this backend.
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    INTERNAL_API_KEY: str = ""

    # ── STAC Catalog DB ───────────────────────────────────────────────────────
    # Write path — pgstac_ingest role (asyncpg, used by FastAPI)
    STAC_DATABASE_URL: str = ""
    # Read-only pool — pgstac_read role (asyncpg, used by FastAPI)
    STAC_READ_URL: str = ""
    # Sync psycopg2 DSN for pypgstac inside Celery workers (pgstac_ingest role).
    # Format: postgresql://pgstac_ingest:password@host:port/pgstac
    STAC_SYNC_DATABASE_URL: str = ""

    # ── Celery / Redis ────────────────────────────────────────────────────────
    # Sync psycopg2 connection for Celery workers (BYPASSRLS role).
    # Never use this URL in API-facing code paths.
    CELERY_DATABASE_URL: str = ""
    # Broker and result backend
    REDIS_URL: str = ""

    # ── Object storage (MinIO in dev, S3 in prod) ─────────────────────────────
    # Internal endpoint used by the API container and Celery workers
    AWS_ENDPOINT_URL: str = ""
    AWS_REGION: str = "us-east-1"
    # Credentials — mapped from MINIO_ROOT_USER/PASSWORD by docker-compose
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    # Path-style is required for MinIO; set to False for virtual-hosted AWS S3
    AWS_S3_FORCE_PATH_STYLE: bool = True
    # Bucket name prefix: full bucket = f"{S3_BUCKET_PREFIX}{org_id}"
    S3_BUCKET_PREFIX: str = "org-"
    # Public URL used in presigned URLs returned to browsers.
    # Must be reachable from the browser, not the container network.
    PUBLIC_MINIO_URL: str = ""

    # ── Internal service URLs ──────────────────────────────────────────────────
    # STAC FastAPI service (stac-fastapi-pgstac)
    STAC_API_URL: str = ""
    # TiTiler (titiler-pgstac) — internal Docker URL; never exposed to browsers
    TITILER_URL: str = ""
    # Public-facing API base URL — used to rewrite TiTiler tile URLs in tilejson
    # responses so browsers call the tile proxy endpoint instead of titiler directly.
    # No trailing slash. Example: https://api.example.com
    PUBLIC_API_URL: str = ""
    MINIO_CORS_ALLOW_ORIGIN: str = ""


    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # Ignore non-app keys injected by docker compose.
    )


settings = Settings()
