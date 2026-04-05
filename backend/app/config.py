"""Application settings from environment (backend/.env when cwd is backend/)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_key: str | None = None
    gemini_api_key: str | None = None
    database_url: str | None = None
    redis_url: str | None = None
    # Legacy / optional; not used by the Gemini pipeline
    anthropic_api_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Singleton-style access for modules that prefer `from app.config import settings`
settings = get_settings()
