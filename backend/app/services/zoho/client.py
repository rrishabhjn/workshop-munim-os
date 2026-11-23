"""Zoho Books India OAuth token cache, HTTP client (401 auto-refresh), and pagination."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
import weakref

import httpx

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
_TOKEN_CACHE = _token_cache
_TOKEN_LOCK = asyncio.Lock()
_COA_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]], Any]] = {}


def _pkg() -> Any:
    return sys.modules[__package__]


async def _get_access_token(force_refresh: bool = False) -> str:
    """Exchange ZOHO_REFRESH_TOKEN for a short-lived Zoho India access token (single-flight locked)."""
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        raise RuntimeError("Zoho Books India credentials are not configured (Wave 2).")

    cache = pkg._token_cache
    now = time.time()
    if (
        not force_refresh
        and cache["access_token"]
        and now < cache["expires_at"] - 60
    ):
        return str(cache["access_token"])

    pre_lock_token = cache.get("access_token")
    async with _TOKEN_LOCK:
        now = time.time()
        if (
            cache["access_token"]
            and now < cache["expires_at"] - 60
            and ((not force_refresh) or cache["access_token"] != pre_lock_token)
        ):
            return str(cache["access_token"])

        url = f"{settings.zoho_accounts_base}/oauth/v2/token"
        form_data = {
            "refresh_token": settings.zoho_refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=form_data)
            if not resp.is_success:
                err_msg = f"HTTP {resp.status_code}"
                try:
                    err_body = resp.json()
                    if isinstance(err_body, dict) and err_body.get("error"):
                        err_msg = str(err_body["error"])
                except Exception:
                    pass
                raise httpx.HTTPStatusError(
                    f"Zoho OAuth token refresh failed ({resp.status_code}: {err_msg})",
                    request=httpx.Request("POST", url),
                    response=resp,
                )
            data = resp.json()

        if not isinstance(data, dict) or not data.get("access_token"):
            err_msg = str(data.get("error") if isinstance(data, dict) else "unknown")
            raise RuntimeError(f"Zoho OAuth token refresh failed: {err_msg}")
        access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        cache["access_token"] = access_token
        cache["expires_at"] = now + expires_in
        return access_token


async def _get_chart_of_accounts(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch Zoho Books Chart of Accounts with a 300s in-process TTL cache."""
    pkg = _pkg()
    settings = pkg.get_settings()
    req_fn = pkg._zoho_request
    cache_key = (str(settings.zoho_org_id or ""), id(req_fn))
    now = time.time()
    entry = pkg._COA_CACHE.get(cache_key)
    if isinstance(entry, tuple) and len(entry) == 3:
        exp, cached_accounts, fn_ref = entry
        if now < float(exp) and (fn_ref is None or fn_ref() is req_fn):
            return list(cached_accounts)
    resp = await req_fn(client, "GET", "/chartofaccounts")
    if not resp.is_success:
        return []
    accounts = list(resp.json().get("chartofaccounts") or [])
    try:
        ref = weakref.ref(req_fn)
    except TypeError:
        ref = None
    pkg._COA_CACHE[cache_key] = (now + 300.0, accounts, ref)
    return list(accounts)


async def _zoho_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Execute a Zoho Books India request with automatic 401 token-refresh retry."""
    pkg = _pkg()
    settings = pkg.get_settings()
    merged_params = {"organization_id": settings.zoho_org_id, **(params or {})}
    url = f"{settings.zoho_books_api_base}/{path.lstrip('/')}"

    for attempt in range(2):
        token = await pkg._get_access_token(force_refresh=(attempt == 1))
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = await client.request(
            method, url, headers=headers, params=merged_params, json=json_body
        )
        if resp.status_code != 401:
            return resp
    return resp


async def _list_all_zoho_docs(
    client: httpx.AsyncClient, path: str, key: str, max_pages: int = 5
) -> tuple[bool, list[dict[str, Any]]]:
    """Fetch paginated Zoho Books invoices or bills safely."""
    pkg = _pkg()
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = await pkg._zoho_request(client, "GET", path, params={"page": page, "per_page": 200})
        if not resp.is_success:
            return False, rows
        data = resp.json()
        rows.extend(data.get(key, []) or [])
        has_more = bool((data.get("page_context") or {}).get("has_more_page"))
        if not has_more:
            break
    return True, rows
