import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.db import get_supabase_admin


@dataclass(frozen=True)
class CurrentUser:
    auth_type: str  # "telegram" | "supabase"
    identifier: str  # telegram_user_id or email
    display_name: str


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    allowed_ids: set[int],
    max_age_seconds: int = 86400,
) -> CurrentUser:
    """Verify Telegram Mini App initData HMAC-SHA256 signature and user allowlist."""
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Telegram bot token is not configured.",
        )

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram initData hash.",
        )

    auth_date_raw = parsed.get("auth_date", "0")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram auth_date.",
        ) from exc

    if time.time() - auth_date > max_age_seconds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram session expired. Please reopen Mini App.",
        )

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram initData signature.",
        )

    try:
        user_obj = json.loads(parsed.get("user", "{}"))
        telegram_user_id = int(user_obj["id"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram user payload.",
        ) from exc

    if allowed_ids and telegram_user_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Telegram user is not allowlisted.",
        )

    name = " ".join(
        part
        for part in [user_obj.get("first_name"), user_obj.get("last_name")]
        if part
    ) or str(telegram_user_id)

    return CurrentUser(
        auth_type="telegram",
        identifier=str(telegram_user_id),
        display_name=name,
    )


def verify_supabase_bearer_token(
    token: str, settings: Settings
) -> CurrentUser:
    """Verify Supabase Auth JWT and check allowlisted email."""
    sb = get_supabase_admin()
    try:
        user_resp = sb.auth.get_user(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Supabase session.",
        ) from exc

    user = getattr(user_resp, "user", None)
    email = (getattr(user, "email", None) or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user has no email.",
        )

    if settings.allowed_email_set and email not in settings.allowed_email_set:
        # Also check app_users table in case allowlist is managed dynamically
        res = (
            sb.table("app_users")
            .select("id, full_name")
            .eq("is_active", True)
            .ilike("email", email)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Email is not allowlisted for Workshop Munim OS.",
            )

    display_name = (
        (getattr(user, "user_metadata", None) or {}).get("full_name")
        or email
    )
    return CurrentUser(
        auth_type="supabase",
        identifier=email,
        display_name=display_name,
    )


async def get_current_user(
    x_telegram_init_data: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Authenticate either Telegram Mini App initData or Supabase Bearer JWT."""
    if x_telegram_init_data:
        return verify_telegram_init_data(
            init_data=x_telegram_init_data,
            bot_token=settings.telegram_bot_token,
            allowed_ids=settings.allowed_telegram_id_set,
        )

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        return verify_supabase_bearer_token(token, settings)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required (Telegram initData or Bearer token).",
    )
