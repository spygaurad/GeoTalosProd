from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_DATABASE_URL: str = "postgresql+asyncpg://app_user:app_pass@app-db:5432/geoplat"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # important: ignore docker env keys we don't map yet
    )


settings = Settings()
