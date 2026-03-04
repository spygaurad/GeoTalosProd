from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    APP_DATABASE_URL: str = "postgresql+asyncpg://app_user:app_pass@app-db:5432/geoplat"
    LOG_LEVEL: str = "INFO"
    # Keep explicit list for readable local-dev defaults. Tighten in deployed environments.
    BACKEND_CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # Ignore non-app keys injected by docker compose.
    )


settings = Settings()
