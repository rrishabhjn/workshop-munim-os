from functools import lru_cache
from supabase import Client, create_client

from app.config import get_settings


@lru_cache
def get_supabase_admin() -> Client:
    """Return a server-side Supabase client using SUPABASE_SECRET_KEY.

    Never expose this key or client to frontend code.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SECRET_KEY must be configured."
        )
    return create_client(settings.supabase_url, settings.supabase_secret_key)
