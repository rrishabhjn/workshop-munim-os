"""CLI-only Zoho OAuth access token tempfile cache (avoids refresh-token rate limits)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from app.services import zoho


def _cache_path() -> Path | None:
    settings = zoho.get_settings()
    if not settings.zoho_enabled or not settings.zoho_refresh_token:
        return None
    digest = hashlib.sha256(settings.zoho_refresh_token.encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"wmos_zoho_tok_{digest}.json"


def load_cli_zoho_token_cache() -> None:
    """Populate in-memory Zoho token cache from user tempfile if valid (>120s left)."""
    path = _cache_path()
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tok = str(data.get("access_token") or "").strip()
        exp = float(data.get("expires_at") or 0.0)
        if tok and exp > time.time() + 120:
            zoho._token_cache["access_token"] = tok
            zoho._token_cache["expires_at"] = exp
    except Exception:
        pass


def save_cli_zoho_token_cache() -> None:
    """Persist valid Zoho access token to user tempfile across short-lived CLI runs."""
    path = _cache_path()
    tok = str(zoho._token_cache.get("access_token") or "").strip()
    exp = float(zoho._token_cache.get("expires_at") or 0.0)
    if path is None or not tok or exp <= time.time() + 60:
        return
    try:
        path.write_text(
            json.dumps({"access_token": tok, "expires_at": exp}),
            encoding="utf-8",
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass
