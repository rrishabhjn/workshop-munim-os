import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi import HTTPException
import pytest

from unittest.mock import AsyncMock, patch
import httpx

from app.auth import verify_telegram_init_data
from app.services import telegram_api, zoho
from app.services.gemini_router import SupplierBillExtraction
from app.services.zoho import create_draft_bill


def _build_signed_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Rishabh"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    payload["hash"] = sig
    return urlencode(payload)


def test_verify_telegram_init_data_valid():
    token = "123456:ABC-DEF-test-token"
    init_data = _build_signed_init_data(token, 111222333)
    user = verify_telegram_init_data(init_data, token, {111222333})
    assert user.auth_type == "telegram"
    assert user.identifier == "111222333"
    assert user.display_name == "Rishabh"


def test_verify_telegram_init_data_rejects_tampered_hash():
    token = "123456:ABC-DEF-test-token"
    init_data = _build_signed_init_data(token, 111222333) + "tampered"
    with pytest.raises(HTTPException) as exc:
        verify_telegram_init_data(init_data, token, {111222333})
    assert exc.value.status_code == 401


def test_verify_telegram_init_data_rejects_unlisted_user():
    token = "123456:ABC-DEF-test-token"
    init_data = _build_signed_init_data(token, 999999999)
    with pytest.raises(HTTPException) as exc:
        verify_telegram_init_data(init_data, token, {111222333})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_zoho_create_draft_bill_stubbed_without_creds():
    from app.config import Settings

    with patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)):
        res = await create_draft_bill(
            supplier_name="Patel Plywood",
            invoice_no="INV-101",
            bill_date="2026-11-15",
            line_items=[],
        )
    assert res["stubbed"] is True
    assert res["status"] == "draft"


@pytest.mark.asyncio
async def test_telegram_markdown_fallback_retries_plain_text():
    req = httpx.Request("POST", "https://api.telegram.org/botx/sendMessage")
    resp_400 = httpx.Response(400, json={"ok": False}, request=req)
    resp_200 = httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=req)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.side_effect = [resp_400, resp_200]

    with (
        patch("app.services.telegram_api._bot_url", return_value="https://api.telegram.org/botx/sendMessage"),
        patch("app.services.telegram_api.httpx.AsyncClient", return_value=mock_client),
    ):
        res = await telegram_api.send_message(chat_id=123, text="bad_markdown *unclosed")

    assert res["ok"] is True
    assert mock_client.post.call_count == 2
    second_payload = mock_client.post.call_args_list[1].kwargs["json"]
    assert "parse_mode" not in second_payload


@pytest.mark.asyncio
async def test_zoho_request_retries_once_on_401():
    req = httpx.Request("GET", "https://www.zohoapis.in/books/v3/bills")
    resp_401 = httpx.Response(401, json={"code": 57}, request=req)
    resp_200 = httpx.Response(200, json={"code": 0, "bills": []}, request=req)

    mock_client = AsyncMock()
    mock_client.request.side_effect = [resp_401, resp_200]

    with patch("app.services.zoho._get_access_token", side_effect=["tok1", "tok2"]) as mock_tok:
        res = await zoho._zoho_request(mock_client, "GET", "/bills")

    assert res.status_code == 200
    assert mock_tok.call_count == 2
    assert mock_tok.call_args_list[1].kwargs["force_refresh"] is True


def test_gemini_response_schemas_accepted_by_google_genai():
    from google.genai import Client, _transformers as t
    from app.services.agent_tools import INTERACTION_TOOLS_SCHEMA

    dummy_client = Client(api_key="test-key")._api_client
    t.t_schema(dummy_client, SupplierBillExtraction)
    assert len(INTERACTION_TOOLS_SCHEMA) == 12
    for tool in INTERACTION_TOOLS_SCHEMA:
        t.t_schema(dummy_client, tool["parameters"])


def test_merge_notes_text():
    assert zoho.merge_notes_text(None, "Regular painter") == "Regular painter"
    assert zoho.merge_notes_text("Regular painter", "regular painter") == "Regular painter"
    assert (
        zoho.merge_notes_text("Regular painter", "Helps with PU polish")
        == "Regular painter; Helps with PU polish"
    )


@pytest.mark.asyncio
async def test_resolve_or_create_contact_ambiguous_and_create():
    with (
        patch("app.services.zoho.get_settings") as mock_cfg,
        patch("app.services.zoho.search_zoho_contacts", return_value=[
            {"contact_id": "c1", "contact_name": "R.K. Sharma"},
            {"contact_id": "c2", "contact_name": "Sharma Ply"},
        ]),
    ):
        mock_cfg.return_value.zoho_enabled = True
        amb = await zoho.resolve_or_create_contact(None, "Sharma", "customer")
    assert amb["status"] == "ambiguous"
    assert len(amb["matches"]) == 2

    with (
        patch("app.services.zoho.get_settings") as mock_cfg,
        patch("app.services.zoho.search_zoho_contacts", return_value=[]),
        patch("app.services.zoho.create_zoho_contact", return_value={"ok": True, "contact": {"contact_id": "c99", "contact_name": "New Mart"}}),
        patch("app.services.zoho._upsert_entity_alias") as mock_alias,
    ):
        mock_cfg.return_value.zoho_enabled = True
        created = await zoho.resolve_or_create_contact(object(), "New Mart", "vendor", gstin="08ABCDE1234F1Z5")
    assert created["status"] == "created"
    assert created["contact_id"] == "c99"
    mock_alias.assert_called_once()


@pytest.mark.asyncio
async def test_public_config_exposes_only_publishable_supabase_fields():
    from app.config import Settings
    from app.main import app

    fake_settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="sb_publishable_test_key",
        supabase_secret_key="sb_secret_DO_NOT_LEAK",
    )
    with patch("app.main.get_settings", return_value=fake_settings):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/public-config")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"supabase_url", "supabase_publishable_key"}
    assert body["supabase_url"] == "https://example.supabase.co"
    assert body["supabase_publishable_key"] == "sb_publishable_test_key"
    assert "sb_secret_DO_NOT_LEAK" not in resp.text


def test_frontend_jsx_components_never_call_hooks_after_early_return():
    """Prevent React error #310 (calling hooks conditionally or after an early return)."""
    from pathlib import Path
    import re

    src_dir = Path(__file__).resolve().parents[2] / "frontend" / "src"
    hook_re = re.compile(r"\b(useState|useEffect|useMemo|useCallback|useRef|useContext|useReducer)\s*\(")
    comp_start_re = re.compile(r"export\s+default\s+function\s+([A-Z]\w*)\s*\(")

    violations: list[str] = []
    for jsx_path in sorted(src_dir.rglob("*.jsx")):
        lines = jsx_path.read_text(encoding="utf-8").splitlines()
        in_component = False
        comp_name = ""
        comp_depth = 0
        seen_early_return = False
        in_block_comment = False

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not in_component:
                m = comp_start_re.search(line)
                if not m:
                    continue
                in_component = True
                comp_name = m.group(1)
                comp_depth = 0
                seen_early_return = False

            depth_at_line_start = comp_depth
            if in_component and depth_at_line_start == 1:
                if re.match(r"^if\s*\(.*\)\s*return\b", stripped) or re.match(r"^if\s*\(", stripped):
                    # Detect single-line or block early returns at component top level
                    if "return" in stripped:
                        seen_early_return = True
                if re.match(r"^return\b", stripped) and stripped in ("return null;", "return;", "return ("):
                    # Unconditional return reached -> anything below won't contain top-level hooks
                    seen_early_return = True

                m_hook = hook_re.search(line)
                if m_hook and not stripped.startswith("//") and seen_early_return:
                    violations.append(
                        f"{jsx_path.relative_to(src_dir)}:{lineno} ({comp_name}) calls {m_hook.group(1)} after early return"
                    )

            # Update brace depth cleanly ignoring strings & comments
            i = 0
            in_str: str | None = None
            while i < len(line):
                ch = line[i]
                nxt = line[i + 1] if i + 1 < len(line) else ""
                if in_block_comment:
                    if ch == "*" and nxt == "/":
                        in_block_comment = False
                        i += 2
                        continue
                    i += 1
                    continue
                if in_str:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == in_str:
                        in_str = None
                    i += 1
                    continue
                if ch == "/" and nxt == "/":
                    break
                if ch == "/" and nxt == "*":
                    in_block_comment = True
                    i += 2
                    continue
                if ch in ('"', "'", "`"):
                    in_str = ch
                    i += 1
                    continue
                if ch == "{":
                    comp_depth += 1
                elif ch == "}":
                    comp_depth -= 1
                    if in_component and comp_depth <= 0 and depth_at_line_start > 0:
                        in_component = False
                        break
                i += 1

    assert violations == [], "React Rules of Hooks violation(s):\n" + "\n".join(violations)
