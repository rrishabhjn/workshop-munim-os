import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi import HTTPException
import pytest

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import httpx

from app.auth import verify_telegram_init_data
from app.routers.telegram import (
    _build_confirm_keyboard,
    _build_contact_picker_keyboard,
    _is_state_expired,
    _normalize_txn_type,
)
from app.services import telegram_api, zoho
from app.services.gemini_router import (
    PhotoClassificationResult,
    SupplierBillExtraction,
    VoiceCaptureResult,
)
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


def test_voice_capture_schema_parses_hinglish_actions():
    raw = json.dumps({
        "intent": "mutation",
        "transcript_summary": "Sharma ji bed polish, 6 ply, 4000 Ramesh ko",
        "actions": [
            {
                "action_type": "update_stage",
                "summary": "Sharma Ji — Teak Bed -> Polish / Upholstery",
                "project_id": "prj-1",
                "item_id": "item-1",
                "stage_code": "polish_upholstery",
            },
            {
                "action_type": "log_cash",
                "summary": "Cash Out ₹4,000 -> Ramesh Mistri",
                "direction": "out",
                "party_or_karigar": "Ramesh Mistri",
                "amount_inr": 4000,
            },
        ],
    })
    parsed = VoiceCaptureResult.model_validate_json(raw)
    assert parsed.intent == "mutation"
    assert len(parsed.actions) == 2


@pytest.mark.asyncio
async def test_zoho_create_draft_bill_stubbed_without_creds():
    res = await create_draft_bill(
        supplier_name="Patel Plywood",
        invoice_no="INV-101",
        bill_date="2026-11-15",
        line_items=[],
    )
    assert res["stubbed"] is True
    assert res["status"] == "draft"


def test_is_state_expired():
    now = datetime.now(timezone.utc)
    assert _is_state_expired({}) is False
    assert _is_state_expired({"created_at": (now - timedelta(minutes=300)).isoformat()}) is False
    assert _is_state_expired({"created_at": (now - timedelta(minutes=365)).isoformat()}) is True


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


def test_normalize_txn_type_and_clarification_keyboard():
    assert _normalize_txn_type("customer_payment", "in") == "customer_receipt"
    assert _normalize_txn_type("rent", "out") == "overhead_expense"
    assert _normalize_txn_type("unknown_label", "out") == "misc"
    assert _normalize_txn_type("unknown_label", "in") == "customer_receipt"

    kb_pick, needs_pick = _build_confirm_keyboard(
        "state-1",
        [{"action_type": "log_cash", "needs_txn_clarification": True}],
    )
    assert needs_pick is True
    assert kb_pick["inline_keyboard"][0][0]["callback_data"] == "txntype:state-1:customer_receipt"

    kb_ok, needs_pick2 = _build_confirm_keyboard(
        "state-1",
        [{"action_type": "log_cash", "needs_txn_clarification": False}],
    )
    assert needs_pick2 is False
    assert kb_ok["inline_keyboard"][0][0]["callback_data"] == "confirm:state-1"


def test_photo_classification_schema_parses_payment_receipt():
    raw = json.dumps({
        "photo_kind": "payment_receipt",
        "reason": "Single UPI payment to Ramesh Mistri",
        "payment_action": {
            "action_type": "log_cash",
            "summary": "UPI Out ₹4,000 -> Ramesh Mistri",
            "direction": "out",
            "txn_type": "karigar_labor",
            "party_or_karigar": "Ramesh Mistri",
            "amount_inr": 4000,
            "payment_mode": "upi",
            "due_date": "2026-11-16",
        },
    })
    parsed = PhotoClassificationResult.model_validate_json(raw)
    assert parsed.photo_kind == "payment_receipt"
    assert parsed.payment_action is not None
    assert parsed.payment_action.payment_mode == "upi"


def test_voice_capture_schema_supports_clarification_needed():
    raw = json.dumps({
        "intent": "clarification_needed",
        "transcript_summary": "5000 mila",
        "answer_text": "Yeh ₹5,000 kis customer se aaya?",
        "actions": [
            {
                "action_type": "log_cash",
                "summary": "Cash In ₹5,000 (customer pending)",
                "direction": "in",
                "amount_inr": 5000,
            }
        ],
    })
    parsed = VoiceCaptureResult.model_validate_json(raw)
    assert parsed.intent == "clarification_needed"
    assert parsed.answer_text is not None
    assert len(parsed.actions) == 1


def test_gemini_response_schemas_accepted_by_google_genai():
    from google.genai import Client, _transformers as t
    from app.services.gemini_router import LearnedRuleExtraction

    dummy_client = Client(api_key="test-key")._api_client
    for schema in (
        VoiceCaptureResult,
        PhotoClassificationResult,
        SupplierBillExtraction,
        LearnedRuleExtraction,
    ):
        t.t_schema(dummy_client, schema)


def test_merge_notes_text_and_upsert_entity_schema():
    assert zoho.merge_notes_text(None, "Regular painter") == "Regular painter"
    assert zoho.merge_notes_text("Regular painter", "regular painter") == "Regular painter"
    assert (
        zoho.merge_notes_text("Regular painter", "Helps with PU polish")
        == "Regular painter; Helps with PU polish"
    )
    raw = json.dumps({
        "intent": "mutation",
        "transcript_summary": "Suresh Ji painter supplier",
        "actions": [
            {
                "action_type": "upsert_entity",
                "summary": "Remember: Suresh Ji (supplier) — Regular painter",
                "party_or_karigar": "Suresh Ji",
                "entity_type": "supplier",
                "notes": "Regular painter, not employee",
            }
        ],
    })
    parsed = VoiceCaptureResult.model_validate_json(raw)
    assert parsed.actions[0].action_type == "upsert_entity"
    assert parsed.actions[0].notes == "Regular painter, not employee"


@pytest.mark.asyncio
async def test_resolve_or_create_contact_ambiguous_and_create():
    kb = _build_contact_picker_keyboard(
        "st-99",
        "Sharma",
        [{"contact_id": "c1", "contact_name": "R.K. Sharma"}, {"contact_id": "c2", "contact_name": "Sharma Ply"}],
    )
    assert kb["inline_keyboard"][0][0]["callback_data"] == "pickcontact:st-99:0"
    assert kb["inline_keyboard"][2][0]["callback_data"] == "pickcontact:st-99:new"

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
