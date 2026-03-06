from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    APP_DATABASE_URL: str = "postgresql+asyncpg://app_user:app_pass@app-db:5432/geoplat"
    # Keep explicit list for readable local-dev defaults. Tighten in deployed environments.
    BACKEND_CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    ENVIRONMENT: str = "development"

    # Clerk — "Frontend API" domain from Clerk Dashboard → API Keys
    # e.g. "happy-fox-42.clerk.accounts.dev"
    CLERK_FRONTEND_API: str = ""
    CLERK_SECRET_KEY: str = ""
    CLERK_WEBHOOK_SECRET: str = ""

    # Shared secret between Next.js webhook relay and this backend.
    # Generate: python -c "import secrets; print(secrets.token_hex(32))"
    INTERNAL_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # Ignore non-app keys injected by docker compose.
    )


settings = Settings()
