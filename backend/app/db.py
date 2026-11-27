from functools import lru_cache
import httpx
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
    client = create_client(settings.supabase_url, settings.supabase_secret_key)
    # Eagerly initialize PostgREST client and switch to HTTP/1.1 connection pooling so
    # concurrent ThreadPoolExecutor reads in _build_compact_grounding never race httpcore's
    # non-thread-safe sync HTTP/2 stream table (KeyError in _response_closed).
    pg = client.postgrest
    old_session = pg.session
    pg.session = httpx.Client(
        base_url=old_session.base_url,
        headers=old_session.headers,
        timeout=old_session.timeout,
        follow_redirects=True,
        http2=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    old_session.close()
    return client
