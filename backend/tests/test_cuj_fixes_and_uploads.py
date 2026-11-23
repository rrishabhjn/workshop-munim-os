"""Comprehensive verification tests for P0-1, P0-2, P1-3, and CUJ #1–#4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.auth import CurrentUser, get_current_user
from app.config import Settings
from app.main import app
from app.routers import telegram as tg_router
from app.services import action_tools, agent_core, draft_cart, goal_intelligence, goal_primitives, step_executors, zoho
from conftest import FakeSupabase


# ---------------------------------------------------------------------------
# P0-1: Zoho Draft Bill Chart of Accounts Mapping
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p01_draft_bill_coa_materials_and_memory_override():
    assert goal_primitives.normalize_or_infer_expense_category("purchase") == "raw_materials"
    assert goal_primitives.normalize_or_infer_expense_category("purchases") == "raw_materials"

    sb = FakeSupabase(
        {
            "workshop_memory": [
                {
                    "id": "wm-1",
                    "category": "account",
                    "is_active": True,
                    "rule_text": "Rajasthan Plastic bills always map to Laminates & Ply COGS account",
                    "created_at": "2026-11-22T10:00:00+00:00",
                }
            ],
            "vendors": [],
            "projects": [],
        }
    )
    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
    )
    posted_payloads: list[dict] = []

    async def fake_zoho_request(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(
                200,
                json={
                    "chartofaccounts": [
                        {"account_id": "coa-mat", "account_name": "Materials", "account_type": "cost_of_goods_sold", "is_active": True},
                        {"account_id": "coa-lam", "account_name": "Laminates & Ply", "account_type": "cost_of_goods_sold", "is_active": True},
                        {"account_id": "coa-freight", "account_name": "Freight & Carriage", "account_type": "expense", "is_active": True},
                    ]
                },
            )
        if method == "POST" and path == "/bills":
            posted_payloads.append(json_body)
            return httpx.Response(200, json={"bill": {"bill_id": "zb-1", "status": "draft"}})
        raise AssertionError(f"Unexpected Zoho request: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_request),
    ):
        # 1) Standard supplier -> Materials COGS, landed cost GST-off
        res1 = await zoho.create_draft_bill(
            supplier_name="In Weave",
            invoice_no="IW-12",
            bill_date="2026-11-22",
            line_items=[
                {"description": "Fabric Roll", "rate": 8000, "quantity": 1},
                {"description": "Freight & GST", "rate": 1440, "quantity": 1},
            ],
            vendor_id="zv-iw",
            expense_category="purchase",
            sb=sb,
        )
        assert res1["expense_account_name"] == "Materials"
        assert posted_payloads[0]["is_inclusive_tax"] is False
        assert all(li["account_id"] == "coa-mat" for li in posted_payloads[0]["line_items"])

        # 2) Vendor matched in workshop_memory category='account' -> overrides CoA
        res2 = await zoho.create_draft_bill(
            supplier_name="Rajasthan Plastic",
            invoice_no="RP-99",
            bill_date="2026-11-22",
            line_items=[{"description": "Acrylic Sheet", "rate": 5500, "quantity": 1}],
            vendor_id="zv-rp",
            sb=sb,
        )
        assert res2["expense_account_name"] == "Laminates & Ply"
        assert posted_payloads[1]["line_items"][0]["account_id"] == "coa-lam"

        # 3) Explicit account_name override wins cleanly
        res3 = await zoho.create_draft_bill(
            supplier_name="Shivam Traders",
            invoice_no="ST-5",
            bill_date="2026-11-22",
            line_items=[{"description": "Hardware", "rate": 2500, "quantity": 1}],
            vendor_id="zv-st",
            account_name="Freight & Carriage",
            sb=sb,
        )
        assert res3["expense_account_name"] == "Freight & Carriage"
        assert posted_payloads[2]["line_items"][0]["account_id"] == "coa-freight"


# ---------------------------------------------------------------------------
# P0-2 & CUJ #1: Telegram Media Sniffing, Errors & Per-User Sequential Queue
# ---------------------------------------------------------------------------
def test_p02_detect_supported_media_mime_magic_bytes():
    assert tg_router._detect_supported_media_mime(b"\xff\xd8\xff\xe0jpg", "application/octet-stream") == "image/jpeg"
    assert tg_router._detect_supported_media_mime(b"\x89PNG\r\n\x1a\npng") == "image/png"
    assert tg_router._detect_supported_media_mime(b"RIFF\x00\x00\x00\x00WEBP", None) == "image/webp"
    assert tg_router._detect_supported_media_mime(b"%PDF-1.7\n...") == "application/pdf"
    assert tg_router._detect_supported_media_mime(b"PK\x03\x04xlsx", "application/vnd.ms-excel", "sheet.xlsx") is None


@pytest.mark.asyncio
async def test_p02_telegram_document_jpeg_unsupported_xlsx_and_exception_reply():
    now_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    # Case 1: JPEG sent as Telegram document -> forwarded to agent_core as image/jpeg
    sb = FakeSupabase(
        {
            "people": [{"id": "p1", "full_name": "Shreyansh", "telegram_user_id": 1001, "is_active": True}],
            "agent_goals": [],
            "telegram_inbox_buffer": [
                {
                    "id": "b1",
                    "telegram_update_id": 201,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "munim",
                    "status": "buffered",
                    "created_at": now_iso,
                    "raw_payload": {
                        "message": {
                            "message_id": 10,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "caption": "Rajasthan Plastic bill",
                            "document": {"file_id": "doc-jpg", "mime_type": "application/octet-stream", "file_name": "bill.bin"},
                        }
                    },
                }
            ],
        }
    )
    captured = {}

    async def fake_run_turn(sb_arg, **kwargs):
        captured.update(kwargs)
        return {"kind": "direct_reply", "reply_text": "Bill staged.", "goals": []}

    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=2.5)),
        patch("app.routers.telegram.telegram_api.download_file_bytes", AsyncMock(return_value=(b"\xff\xd8\xff\xe0jpegbytes", "documents/file_0.jpg"))),
        patch("app.routers.telegram.telegram_api.send_message", AsyncMock(return_value={"result": {"message_id": 91}})),
        patch("app.routers.telegram.agent_core.run_agent_turn", side_effect=fake_run_turn),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert captured["media_files"] == [(b"\xff\xd8\xff\xe0jpegbytes", "image/jpeg")]
    assert sb.store["telegram_inbox_buffer"][0]["status"] == "processed"

    # Case 2: Unsupported .xlsx only -> polite Hinglish reply, no crash
    sb2 = FakeSupabase(
        {
            "people": [{"id": "p1", "full_name": "Shreyansh", "telegram_user_id": 1001, "is_active": True}],
            "agent_goals": [],
            "telegram_inbox_buffer": [
                {
                    "id": "b2",
                    "telegram_update_id": 202,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "munim",
                    "status": "buffered",
                    "created_at": now_iso,
                    "raw_payload": {
                        "message": {
                            "message_id": 11,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "document": {"file_id": "doc-xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "file_name": "ledger.xlsx"},
                        }
                    },
                }
            ],
        }
    )
    send_mock = AsyncMock(return_value={"result": {"message_id": 92}})
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb2),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=2.5)),
        patch("app.routers.telegram.telegram_api.download_file_bytes", AsyncMock(return_value=(b"PK\x03\x04excel", "documents/ledger.xlsx"))),
        patch("app.routers.telegram.telegram_api.send_message", send_mock),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert "support nahi hai" in send_mock.call_args.kwargs["text"]
    assert sb2.store["telegram_inbox_buffer"][0]["status"] == "processed"

    # Case 3: Unhandled exception -> marks failed AND sends visible Hinglish error reply
    sb3 = FakeSupabase(
        {
            "people": [{"id": "p1", "full_name": "Shreyansh", "telegram_user_id": 1001, "is_active": True}],
            "agent_goals": [],
            "telegram_inbox_buffer": [
                {
                    "id": "b3",
                    "telegram_update_id": 203,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "munim",
                    "status": "buffered",
                    "created_at": now_iso,
                    "raw_payload": {"message": {"message_id": 12, "chat": {"id": -9001, "type": "supergroup"}, "from": {"id": 1001, "first_name": "Shreyansh"}, "text": "Hello bill"}},
                }
            ],
        }
    )
    err_send = AsyncMock(return_value={"result": {"message_id": 93}})
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb3),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=2.5)),
        patch("app.routers.telegram.telegram_api.send_message", err_send),
        patch("app.routers.telegram.agent_core.run_agent_turn", AsyncMock(side_effect=RuntimeError("Boom"))),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert sb3.store["telegram_inbox_buffer"][0]["status"] == "failed"
    assert "error aa gaya" in err_send.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cuj1_sequential_per_user_queue_drains_buffered_after_active_claim():
    now_utc = datetime.now(timezone.utc)
    # 1) While Turn 1 is actively 'claimed' (< 60s), a secondary worker must return empty so Turn 2 waits in line
    sb_busy = FakeSupabase(
        {
            "telegram_inbox_buffer": [
                {"id": "t1", "chat_id": -9001, "user_id": 1001, "status": "claimed", "claimed_at": (now_utc - timedelta(seconds=5)).isoformat(), "created_at": (now_utc - timedelta(seconds=10)).isoformat()},
                {"id": "t2", "chat_id": -9001, "user_id": 1001, "status": "buffered", "created_at": now_utc.isoformat()},
            ]
        }
    )
    claimed = await tg_router._claim_next_telegram_burst(sb_busy, -9001, 1001, 0.0, apply_debounce=False)
    assert claimed == []

    # 2) Active worker finishes Turn 1, then drains Turn 2 inserted during Turn 1 cleanly
    sb_seq = FakeSupabase(
        {
            "people": [{"id": "p1", "full_name": "Shreyansh", "telegram_user_id": 1001, "is_active": True}],
            "agent_goals": [],
            "telegram_inbox_buffer": [
                {
                    "id": "m1",
                    "telegram_update_id": 301,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "munim",
                    "status": "buffered",
                    "created_at": (now_utc - timedelta(seconds=10)).isoformat(),
                    "raw_payload": {"message": {"message_id": 21, "chat": {"id": -9001, "type": "supergroup"}, "from": {"id": 1001, "first_name": "Shreyansh"}, "text": "Paid 800 Iliyas"}},
                }
            ],
        }
    )
    turn_texts: list[str] = []

    async def fake_sequential_turn(sb_arg, **kwargs):
        turn_texts.append(kwargs.get("text") or "")
        if len(turn_texts) == 1:
            # Simulate user typing "In cash" while Turn 1 LLM call is running
            sb_seq.store["telegram_inbox_buffer"].append(
                {
                    "id": "m2",
                    "telegram_update_id": 302,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "munim",
                    "status": "buffered",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "raw_payload": {"message": {"message_id": 22, "chat": {"id": -9001, "type": "supergroup"}, "from": {"id": 1001, "first_name": "Shreyansh"}, "text": "In cash"}},
                }
            )
        return {"kind": "direct_reply", "reply_text": f"Ack #{len(turn_texts)}", "goals": []}

    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb_seq),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.routers.telegram.telegram_api.send_message", AsyncMock(return_value={"result": {"message_id": 95}})),
        patch("app.routers.telegram.agent_core.run_agent_turn", side_effect=fake_sequential_turn),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert turn_texts == ["Paid 800 Iliyas", "In cash"]
    assert all(r["status"] == "processed" for r in sb_seq.store["telegram_inbox_buffer"])


# ---------------------------------------------------------------------------
# CUJ #2: Same-Day IST Financial Undo & Amendments
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cuj2_same_day_financial_undo_and_amendment_flow():
    now_utc = datetime.now(timezone.utc)
    yesterday_utc = now_utc - timedelta(days=2)
    gid_today = "aaaa1111-1111-1111-1111-111111111101"
    gid_old = "aaaa1111-1111-1111-1111-111111111102"
    pid = "22222222-2222-2222-2222-222222222201"

    sb = FakeSupabase(
        {
            "projects": [{"id": pid, "project_code": "PRJ-201", "customer_name": "Rajyawas Customer", "cost_cache_inr": 4000.0, "status": "active", "pipeline_stage": "execution"}],
            "payment_accounts": [
                {"id": "acc-cash", "code": "cash_galla", "display_name": "Cash Galla", "account_kind": "cash_galla", "aliases": ["cash"], "is_active": True},
                {"id": "acc-rohit", "code": "float_rohit", "display_name": "Rohit Wallet", "account_kind": "supervisor_float", "aliases": ["rohit"], "is_active": True},
            ],
            "agent_goals": [
                {
                    "id": gid_today,
                    "status": "completed",
                    "goal_type": "munim_entry",
                    "summary_title": "Expense: Iliyas",
                    "resolved_at": now_utc.isoformat(),
                    "updated_at": now_utc.isoformat(),
                    "collected_slots": {
                        "operation_kind": "expense",
                        "party_name": "Iliyas",
                        "amount_inr": 4000.0,
                        "payment_account_code": "cash_galla",
                        "payment_account_id": "acc-cash",
                        "project_id": pid,
                    },
                    "steps": [
                        {
                            "step_index": 0,
                            "target": "zoho",
                            "operation": "expense",
                            "status": "succeeded",
                            "payload": {},
                            "result": {
                                "ok": True,
                                "zoho_id": "ze-old-4000",
                                "zoho_type": "expense",
                                "project_undo_snapshot": {"project_id": pid, "before": {"cost_cache_inr": 0.0}},
                            },
                        }
                    ],
                },
                {
                    "id": gid_old,
                    "status": "completed",
                    "goal_type": "munim_entry",
                    "summary_title": "Old Expense",
                    "resolved_at": yesterday_utc.isoformat(),
                    "updated_at": yesterday_utc.isoformat(),
                    "collected_slots": {"operation_kind": "expense", "party_name": "Old", "amount_inr": 500.0},
                    "steps": [{"step_index": 0, "operation": "expense", "status": "succeeded", "result": {"ok": True, "zoho_id": "ze-old", "zoho_type": "expense"}}],
                },
            ],
        }
    )

    assert agent_core.is_goal_undoable_today_ist(sb.store["agent_goals"][0]) is True
    assert agent_core.is_goal_undoable_today_ist(sb.store["agent_goals"][1]) is False

    # Past-midnight IST entry refused safely
    old_res = await agent_core.undo_completed_goal_async(sb, goal_id=gid_old, actor_person_id="p-shr")
    assert old_res["ok"] is False
    assert "same-day IST" in old_res["error"]

    # Implicit amendment detection catches follow-up correction against today's completed goal
    item = agent_core.GoalIntentItem(
        operation_kind="expense",
        summary_title="Expense: Iliyas",
        party_name="Iliyas",
        payment_account_code="float_rohit",
    )
    op2, amend_slots, amend_choices = await agent_core.prepare_turn_financial_amendment_and_choices(
        sb,
        op="expense",
        slots={"operation_kind": "expense", "party_name": "Iliyas", "payment_account_code": "float_rohit", "payment_account_id": "acc-rohit"},
        item=item,
        user_text="Actually Iliyas wala 4000 Rohit wallet se tha",
        heard_transcript=None,
        choices_serialized=[],
    )
    assert op2 == "expense"
    assert amend_slots["amends_goal_id"] == gid_today
    assert amend_slots["amount_inr"] == 4000.0
    assert any("Log Separate New Entry" in c["label"] for c in amend_choices)

    steps = agent_core._build_steps_for_slots(op2, amend_slots)
    assert [s["operation"] for s in steps] == ["void_prior_goal", "expense"]

    # Tapping [➕ Log Separate New Entry] refreshes Confirm card without void_prior_goal
    new_gid = "aaaa1111-1111-1111-1111-111111111103"
    sb.store["agent_goals"].append(
        {
            "id": new_gid,
            "status": "awaiting_confirmation",
            "goal_type": "munim_entry",
            "summary_title": "Expense: Iliyas",
            "collected_slots": amend_slots,
            "steps": steps,
            "metadata": {},
        }
    )
    sep_out = await agent_core.apply_slot_patch_and_resume(
        sb,
        goal_id=new_gid,
        slot_patch={"amends_goal_id": None, "amend_prior_summary": None, "skip_amend_check": True},
        actor_person_id="p-shr",
        actor_label="Shreyansh",
    )
    assert sep_out["status"] == "awaiting_confirmation"
    assert "amends_goal_id" not in sep_out["collected_slots"]

    # Confirming the original amendment goal runs void_prior_goal -> DELETE Zoho + revert snapshot -> save new entry!
    sb.store["agent_goals"][2]["collected_slots"] = amend_slots
    sb.store["agent_goals"][2]["steps"] = steps

    deleted_calls: list[tuple[str, str]] = []

    async def fake_delete(*, zoho_id=None, zoho_type=None):
        deleted_calls.append((zoho_id, zoho_type))
        return {"ok": True, "deleted": True}

    with (
        patch("app.services.zoho.delete_zoho_financial_entry", side_effect=fake_delete),
        patch(
            "app.services.zoho.push_management_entry_to_zoho",
            AsyncMock(return_value={"ok": True, "status_code": 200, "zoho_id": "ze-new-4000", "zoho_type": "expense"}),
        ),
    ):
        conf_res = await agent_core.confirm_and_execute_goal(
            sb, goal_id=new_gid, actor_person_id="p-shr", actor_label="Shreyansh"
        )
        assert conf_res["status"] == "completed"
        assert conf_res["undo_available"] is True
        assert deleted_calls == [("ze-old-4000", "expense")]
        assert sb.store["agent_goals"][0]["status"] == "undone"

        # And same-day [↩️ Undo] on the new completed goal ALSO reverses it in Zoho!
        undo_res = await agent_core.undo_completed_goal_async(sb, goal_id=new_gid, actor_person_id="p-shr")
        assert undo_res["ok"] is True
        assert deleted_calls[-1] == ("ze-new-4000", "expense")
        assert sb.store["projects"][0]["cost_cache_inr"] == 0.0


# ---------------------------------------------------------------------------
# CUJ #3: Custodian Float Default, Payee Hygiene, Site Linking, Dedup & P&L
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cuj3_custodian_float_payee_hygiene_site_linking_and_vendor_dedup():
    cust_raj_id = "cccc1111-1111-1111-1111-111111111101"
    sb = FakeSupabase(
        {
            "people": [{"id": "p-ansh", "full_name": "Ansh", "person_type": "operator", "aliases": [], "is_active": True}],
            "payment_accounts": [
                {"id": "acc-cash", "code": "cash_galla", "display_name": "Cash Galla", "account_kind": "cash_galla", "is_active": True},
                {"id": "acc-ansh", "code": "float_ansh", "display_name": "Ansh Wallet", "account_kind": "supervisor_float", "custodian_person_id": "p-ansh", "aliases": ["ansh"], "is_active": True},
            ],
            "customers": [
                {"id": cust_raj_id, "full_name": "Rajyawas Customer", "area_landmark": "Rajyawas", "aliases": ["Rajyawas"], "zoho_contact_id": "zc-raj", "is_active": True}
            ],
            "vendors": [
                {"id": "v-1", "canonical_name": "Patel Ply House", "vendor_kind": "supplier", "gstin": "08ABCDE1234F1Z5", "aliases": ["Patel Ply"]}
            ],
            "projects": [],
            "agent_goals": [],
        }
    )

    offline_settings = Settings(zoho_client_id="", zoho_client_secret="", zoho_refresh_token="", zoho_org_id="")
    with patch("app.services.zoho.get_settings", return_value=offline_settings):
        # 3A-1: Outgoing to Ansh without salary word -> float_transfer + direct expense toggle
        op_a, sl_a, ch_a = await agent_core.prepare_turn_financial_amendment_and_choices(
            sb,
            op="expense",
            slots={"operation_kind": "expense", "party_name": "Ansh", "person_id": "p-ansh", "amount_inr": 5000.0, "payment_account_code": "cash_galla", "payment_account_id": "acc-cash"},
            item=agent_core.GoalIntentItem(operation_kind="expense", summary_title="Ansh"),
            user_text="5000 paid to Ansh from Cash Galla",
            heard_transcript=None,
            choices_serialized=[],
        )
        assert op_a == "float_transfer"
        assert sl_a["to_payment_account_code"] == "float_ansh"
        assert any("Book as Direct Expense" in c["label"] for c in ch_a)

        # 3A-2: Explicit salary stays direct expense
        op_sal, _, _ = await agent_core.prepare_turn_financial_amendment_and_choices(
            sb,
            op="expense",
            slots={"operation_kind": "expense", "party_name": "Ansh", "person_id": "p-ansh", "amount_inr": 15000.0, "expense_category": "salary_wages", "payment_account_code": "cash_galla", "payment_account_id": "acc-cash"},
            item=agent_core.GoalIntentItem(operation_kind="expense", summary_title="Ansh Salary"),
            user_text="Ansh salary 15000 cash",
            heard_transcript=None,
            choices_serialized=[],
        )
        assert op_sal == "expense"

        # 3B-1: Generic word "Fuel" never marks is_new_payee=True
        _, sl_fuel, _ = await agent_core.prepare_turn_financial_amendment_and_choices(
            sb,
            op="expense",
            slots={"operation_kind": "expense", "party_name": "Fuel", "amount_inr": 600.0, "payment_account_code": "cash_galla", "payment_account_id": "acc-cash", "is_new_payee": True},
            item=agent_core.GoalIntentItem(operation_kind="expense", summary_title="Fuel"),
            user_text="Fuel 600 cash",
            heard_transcript=None,
            choices_serialized=[],
        )
        assert sl_fuel["is_generic_expense_label"] is True
        assert sl_fuel["is_new_payee"] is False
        assert sl_fuel["vendor_id"] is None

        # 3B-2: Customer matched as site with 0 orders -> customer_id set, vendor_id=None, offers [➕ Create Order]
        _, sl_raj, ch_raj = await agent_core.prepare_turn_financial_amendment_and_choices(
            sb,
            op="expense",
            slots={"operation_kind": "expense", "party_name": "Rajyawas Customer", "amount_inr": 1500.0, "payment_account_code": "cash_galla", "payment_account_id": "acc-cash", "notes": "Tempo bhada", "is_new_payee": True},
            item=agent_core.GoalIntentItem(operation_kind="expense", summary_title="Rajyawas tempo"),
            user_text="Rajyawas Customer tempo 1500 cash",
            heard_transcript=None,
            choices_serialized=[],
        )
        assert sl_raj["customer_id"] == cust_raj_id
        assert sl_raj["vendor_id"] is None
        assert sl_raj["is_new_payee"] is False
        assert sl_raj["matched_customer_as_site"] is True
        assert any("Create Order" in c["label"] for c in ch_raj)

        # Enabling auto_create_site_order builds [auto_create_site_order, expense] & updates cost_cache_inr
        sl_raj["auto_create_site_order"] = True
        steps_raj = agent_core._build_steps_for_slots("expense", sl_raj)
        assert [s["operation"] for s in steps_raj] == ["auto_create_site_order", "expense"]
        gid_raj = "bbbb1111-1111-1111-1111-111111111101"
        sb.store["agent_goals"].append(
            {"id": gid_raj, "status": "awaiting_confirmation", "goal_type": "munim_entry", "summary_title": "Rajyawas", "collected_slots": sl_raj, "steps": steps_raj, "metadata": {}}
        )
        with patch(
            "app.services.zoho.push_management_entry_to_zoho",
            AsyncMock(return_value={"ok": True, "status_code": 200, "zoho_id": "ze-raj", "zoho_type": "expense"}),
        ):
            r_raj = await agent_core.confirm_and_execute_goal(sb, goal_id=gid_raj, actor_person_id="p-ansh", actor_label="Ansh")
            assert r_raj["status"] == "completed"
            assert len(sb.store["projects"]) == 1
            assert sb.store["projects"][0]["cost_cache_inr"] == 1500.0

    # 3B-3: Vendor dedup by GSTIN & alias prevents duplicate vendors
    dedup_gst = agent_core.dedup_or_insert_vendor_or_person(
        sb, {"person_or_vendor_kind": "supplier", "full_name": "Patel Plywoods", "gstin": "08abcde1234f1z5", "phone": "9999999999"}
    )
    assert dedup_gst["deduped"] is True
    assert dedup_gst["vendor_id"] == "v-1"
    assert len(sb.store["vendors"]) == 1


# ---------------------------------------------------------------------------
# CUJ #4: Compound New Order + Initial Advance Single Confirm Card
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cuj4_compound_new_order_plus_initial_advance_single_card():
    sb = FakeSupabase(
        {
            "customers": [],
            "projects": [],
            "project_items": [],
            "stage_events": [],
            "agent_goals": [],
            "payment_accounts": [
                {"id": "acc-axis", "code": "axis", "display_name": "Axis Bank", "account_kind": "bank", "aliases": ["axis"], "is_active": True}
            ],
        }
    )
    # 1) Same-turn split calls (new_lead + customer_receipt) fold cleanly into 1 combined item
    cart = draft_cart.TurnDraftCart()
    acc_map = {"axis": "acc-axis"}
    await action_tools.execute_action_tool(
        sb,
        "stage_new_order",
        {
            "full_name": "Suresh Verma",
            "area_landmark": "Civil Lines, Ajmer",
            "rough_scope": "Kitchen + 2 Wardrobes",
            "quoted_total": 350000,
            "items": [{"item_name": "Modular Kitchen"}, {"item_name": "Master Wardrobe"}],
        },
        cart=cart,
        acc_code_to_id=acc_map,
        ui_context=None,
        user_text="New order Suresh Verma Civil Lines Ajmer Kitchen + 2 Wardrobes deal 3.5L advance 50k Axis",
        heard_transcript=None,
    )
    await action_tools.execute_action_tool(
        sb,
        "stage_financial_entry",
        {
            "entry_kind": "customer_receipt",
            "party_name": "Suresh Verma",
            "amount_inr": 50000,
            "payment_account_code": "axis",
            "notes": "Initial token advance",
        },
        cart=cart,
        acc_code_to_id=acc_map,
        ui_context=None,
        user_text="New order Suresh Verma Civil Lines Ajmer Kitchen + 2 Wardrobes deal 3.5L advance 50k Axis",
        heard_transcript=None,
    )
    folded_items = cart.items()
    assert len(folded_items) == 1
    assert folded_items[0].operation_kind == "new_lead"
    assert folded_items[0].advance_amount_inr == 50000.0
    assert folded_items[0].payment_account_code == "axis"

    # 2) Missing payment_account_code requested deterministically if omitted with advance
    assert "payment_account_code" in agent_core._compute_missing_slots_for_op(
        "new_lead",
        {"full_name": "Suresh", "area_landmark": "Ajmer", "rough_scope": "Kitchen", "advance_amount_inr": 50000},
    )

    # 3) Compiled sequential steps: new_lead -> 2 items -> site_sizing_boq -> customer_receipt
    slots = folded_items[0].model_dump()
    slots["payment_account_id"] = "acc-axis"
    steps = agent_core._build_steps_for_slots("new_lead", slots)
    assert [s["operation"] for s in steps] == [
        "new_lead",
        "item_upsert_or_move",
        "item_upsert_or_move",
        "pipeline_stage_move",
        "customer_receipt",
    ]
    assert steps[3]["payload"]["to_stage"] == "site_sizing_boq"

    gid = "dddd1111-1111-1111-1111-111111111101"
    sb.store["agent_goals"].append(
        {
            "id": gid,
            "status": "awaiting_confirmation",
            "domain_scope": "production",
            "goal_type": "order_onboarding",
            "summary_title": "Suresh Verma + Advance",
            "collected_slots": slots,
            "steps": steps,
            "metadata": {},
        }
    )
    with patch(
        "app.services.zoho.push_management_entry_to_zoho",
        AsyncMock(return_value={"ok": True, "status_code": 200, "zoho_id": "zp-adv-1", "zoho_type": "sales_deposit"}),
    ):
        res = await agent_core.confirm_and_execute_goal(
            sb, goal_id=gid, actor_person_id="p-shr", actor_label="Shreyansh"
        )
        assert res["status"] == "completed"
        prj = sb.store["projects"][0]
        assert prj["pipeline_stage"] == "site_sizing_boq"
        assert prj["payment_stage"] == "advance_in"
        assert prj["paid_cache_inr"] == 50000.0
        assert len(sb.store["project_items"]) == 2
        # Even if Zoho reporting tag created on paid lead/sizing order, post-save verifier passes cleanly
        prj["zoho_tag_option_id"] = "opt-99"
        v = goal_intelligence.verify_committed_goal_steps(sb, steps=[{"status": "succeeded", "operation": "new_lead", "result": {"project_id": prj["id"], "customer_id": prj["customer_id"]}}], slots=slots)
        assert v["verified"] is True


# ---------------------------------------------------------------------------
# P1-3: Optional Order/Item Photo Uploads & Telegram Item Photo Auto-Link
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p13_order_attachment_item_photo_endpoints_and_telegram_pre_pack():
    pid = "eeee1111-1111-1111-1111-111111111101"
    iid = "ffff1111-1111-1111-1111-111111111101"
    sb = FakeSupabase(
        {
            "people": [{"id": "p-shr", "full_name": "Shreyansh", "email": "shr@example.com", "person_type": "operator", "is_active": True}],
            "app_users": [{"email": "shr@example.com", "is_active": True, "can_munim": True}],
            "projects": [{"id": pid, "project_code": "PRJ-301", "customer_name": "Kiran Ji", "payment_stage": "advance_in", "metadata": {}}],
            "project_items": [{"id": iid, "project_id": pid, "item_name": "TV Unit", "stage": "in_shop", "metadata": {}}],
            "stage_events": [],
        }
    )

    # 1) Telegram / AgentCore item image upload auto-populates pre_pack_photo_path
    tg_res = await step_executors.upsert_or_transition_item(
        sb,
        project_id=pid,
        item_id=iid,
        to_stage="packed_ready",
        actor_person_id="p-shr",
        uploaded_gcs_paths=["orders/PRJ-301/pack-1.webp"],
    )
    assert tg_res["ok"] is True
    assert sb.store["project_items"][0]["pre_pack_photo_path"] == "orders/PRJ-301/pack-1.webp"

    # 2) Direct web multipart uploads via /api/production/orders/{id}/attachments and /items/{id}/photo
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="google", identifier="shr@example.com", display_name="Shreyansh"
    )
    try:
        with (
            patch("app.routers.domains.get_supabase_admin", return_value=sb),
            patch("app.routers.domains.storage.upload_image_as_webp", side_effect=lambda path, raw: path),
            patch("app.routers.domains.storage.upload_bytes", side_effect=lambda path, raw, content_type="": path),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r_att = await client.post(
                    f"/api/production/orders/{pid}/attachments",
                    files={"file": ("sizing.pdf", b"%PDF-1.5\n...", "application/pdf")},
                )
                assert r_att.status_code == 200
                assert r_att.json()["path"].startswith(f"orders/{pid}/spec-")
                assert len(sb.store["projects"][0]["metadata"]["uploaded_gcs_paths"]) == 2

                r_photo = await client.post(
                    f"/api/production/items/{iid}/photo",
                    files={"file": ("tv.jpg", b"\xff\xd8\xff\xe0jpg", "image/jpeg")},
                )
                assert r_photo.status_code == 200
                new_path = r_photo.json()["pre_pack_photo_path"]
                assert new_path.startswith(f"items/{iid}/photo-")
                assert sb.store["project_items"][0]["pre_pack_photo_path"] == new_path

                r_bad = await client.post(
                    f"/api/production/items/{iid}/photo",
                    files={"file": ("doc.txt", b"hello", "text/plain")},
                )
                assert r_bad.status_code == 400
    finally:
        app.dependency_overrides.clear()
