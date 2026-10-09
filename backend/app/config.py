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
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
        self.gemini_api_keys = [
            key.strip()
            for key in os.environ.get("GEMINI_API_KEYS", "").split(",")
            if key.strip()
        ]
        self.gemini_model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        self.stripe_secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
        self.stripe_webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        self.stripe_price_id = os.environ.get("STRIPE_PRICE_ID", "")
        self.frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
        self.qa_bypass_emails = {
            email.strip().lower()
            for email in os.environ.get("QA_BYPASS_EMAILS", "").split(",")
            if email.strip()
        }
