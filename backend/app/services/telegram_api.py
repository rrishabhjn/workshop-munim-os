import logging
from typing import Any
import httpx

from app.config import get_settings

TELEGRAM_API_BASE = "https://api.telegram.org"
logger = logging.getLogger(__name__)


def _bot_url(method: str) -> str:
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


async def _post_with_markdown_fallback(
    method: str, payload: dict[str, Any], timeout: float = 15.0
) -> dict[str, Any]:
    """POST to Telegram Bot API; retry as plain text if Markdown entity parsing fails."""
    url = _bot_url(method)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code == 400 and payload.get("parse_mode"):
            plain_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
            resp = await client.post(url, json=plain_payload)
        resp.raise_for_status()
        return resp.json()


async def send_message(
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "Markdown",
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    return await _post_with_markdown_fallback("sendMessage", payload)


async def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str = "Markdown",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return await _post_with_markdown_fallback("editMessageText", payload)


async def answer_callback_query(
    callback_query_id: str, text: str = ""
) -> None:
    payload = {"callback_query_id": callback_query_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _bot_url("answerCallbackQuery"), json=payload
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("answerCallbackQuery failed (possibly expired): %s", exc)


async def download_file_bytes(file_id: str) -> tuple[bytes, str]:
    """Fetch a Telegram file by file_id and return (bytes, file_path)."""
    token = get_settings().telegram_bot_token
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta_resp = await client.get(
            _bot_url("getFile"), params={"file_id": file_id}
        )
        meta_resp.raise_for_status()
        file_path = meta_resp.json()["result"]["file_path"]

        dl_url = f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"
        file_resp = await client.get(dl_url)
        file_resp.raise_for_status()
        return file_resp.content, file_path
