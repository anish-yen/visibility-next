import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@lru_cache
def get_settings():
    return Settings()


class Settings:
    """Application settings from environment."""

    def __init__(self) -> None:
        self.supabase_url = os.environ.get("SUPABASE_URL", "")
        self.supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
        self.supabase_service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.database_url = os.environ.get("DATABASE_URL", "")
        self.redis_url = os.environ.get("REDIS_URL", "")
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
