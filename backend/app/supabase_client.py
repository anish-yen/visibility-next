from supabase import Client, create_client

from app.config import get_settings


def get_supabase_admin() -> Client:
    """Server-side Supabase client with service role (bypasses RLS)."""
    s = get_settings()
    if not s.supabase_url or not s.supabase_service_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(s.supabase_url, s.supabase_service_key)
