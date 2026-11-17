"""Tests for Munim financial slot extraction, Zoho tags/CoA/wallets sync, Google Sheets backup, and Telegram burst claims."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.routers import telegram as tg_router
from app.services import agent_core, goal_primitives, sheets_backup, zoho
from conftest import FakeSupabase


def test_financial_slot_backfill_extracts_amount_and_cash_account():
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {"code": "cash_galla", "display_name": "Cash Galla", "aliases": ["cash", "galla", "nakad"], "is_active": True},
                {"code": "rgb", "display_name": "RGB (Gramin Bank)", "aliases": ["rgb", "gramin bank"], "is_active": True},
            ],
            "projects": [
                {"id": "7923bc50-77dc-4779-9a73-61cf722c318e", "customer_name": "Praveen Mehta"}
            ],
        }
    )
    slots = {
        "operation_kind": "customer_receipt",
        "project_id": "7923bc50-77dc-4779-9a73-61cf722c318e",
        "summary_title": "Praveen Mehta se ₹20,000 advance Cash Galla mein receive",
    }
    agent_core._backfill_financial_slots_from_text(
        sb,
        slots,
        user_text="Praveen Mehta Advance Received 20,000 cash",
        heard_transcript=None,
    )
    assert slots["amount_inr"] == 20000.0
    assert slots["payment_account_code"] == "cash_galla"
    assert slots["party_name"] == "Praveen Mehta"

    slots_k = {"operation_kind": "customer_receipt", "summary_title": "Received 50k in RGB"}
    agent_core._backfill_financial_slots_from_text(
        sb, slots_k, user_text="50k rgb", heard_transcript=None
    )
    assert slots_k["amount_inr"] == 50000.0
    assert slots_k["payment_account_code"] == "rgb"


@pytest.mark.asyncio
async def test_pass1_zoho_reporting_tags_draft_bill_and_pnl_sync():
    pid = "11111111-1111-1111-1111-111111111101"
    cid = "22222222-2222-2222-2222-222222222201"
    sb = FakeSupabase(
        {
            "customers": [
                {"id": cid, "full_name": "Mehta Garment", "area_landmark": "Sendra Road", "phone": "9829000001"}
            ],
            "projects": [
                {
                    "id": pid,
                    "project_code": "PRJ-101",
                    "cf_project_code": "PRJ-101",
                    "customer_id": cid,
                    "customer_name": "Mehta Garment",
                    "phone": "9829000001",
                    "status": "active",
                    "project_type": "customer",
                    "zoho_contact_id": "zc-1",
                    "zoho_tag_id": None,
                    "zoho_tag_option_id": None,
                    "zoho_project_id": None,
                }
            ],
            "entity_dictionary": [],
        }
    )

    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
        zoho_project_tag_id="",
    )

    async def fake_zoho_request(client, method, path, *, params=None, json_body=None):
        assert "/settings/tags" not in path
        if method == "GET" and path == "/reportingtags":
            return httpx.Response(200, json={"reporting_tags": [{"tag_id": "tag-prj", "tag_name": "Project", "is_active": True}]})
        if method == "GET" and path == "/reportingtags/tag-prj":
            return httpx.Response(200, json={"reporting_tag": {"tag_id": "tag-prj", "tag_options": []}})
        if method == "GET" and path == "/reportingtags/tag-prj/options/all":
            return httpx.Response(200, json={"tag_options": []})
        if method == "POST" and path == "/reportingtags/tag-prj/options":
            return httpx.Response(200, json={"tag_option": {"tag_option_id": "opt-101", "tag_option_name": "PRJ-101 - Mehta Garment"}})
        if method == "GET" and path == "/bills" and (params or {}).get("reference_number") == "MUNIM-G1-S1":
            return httpx.Response(200, json={"bills": []})
        if method == "GET" and path == "/bills" and (params or {}).get("reference_number") == "MUNIM-DUP-S0":
            return httpx.Response(200, json={"bills": [{"bill_id": "zb-existing", "reference_number": "MUNIM-DUP-S0"}]})
        if method == "POST" and path == "/bills":
            assert json_body["status"] == "draft"
            assert json_body["reference_number"] == "MUNIM-G1-S1"
            assert json_body["tags"] == [{"tag_id": "tag-prj", "tag_option_id": "opt-101"}]
            assert json_body["line_items"][0]["tags"] == [{"tag_id": "tag-prj", "tag_option_id": "opt-101"}]
            assert json_body["custom_fields"] == [{"api_name": "cf_project_code", "value": "PRJ-101"}]
            return httpx.Response(200, json={"bill": {"bill_id": "zb-new", "status": "draft"}})
        if method == "GET" and path == "/invoices":
            return httpx.Response(
                200,
                json={
                    "invoices": [
                        {"invoice_id": "i1", "status": "sent", "total": 120000, "tags": [{"tag_option_id": "opt-101"}]},
                        {"invoice_id": "i2", "status": "void", "total": 99999, "tags": [{"tag_option_id": "opt-101"}]},
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        if method == "GET" and path == "/bills":
            return httpx.Response(
                200,
                json={
                    "bills": [
                        {"bill_id": "b1", "status": "draft", "total": 45000, "custom_field_hash": {"cf_project_code": "PRJ-101"}},
                        {"bill_id": "b2", "status": "void", "total": 5000, "tags": [{"tag_option_id": "opt-101"}]},
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        raise AssertionError(f"Unexpected Zoho call: {method} {path} params={params}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_request),
    ):
        bridge = await zoho.ensure_project_zoho_bridge(sb, pid)
        assert bridge["zoho_tag_id"] == "tag-prj"
        assert bridge["zoho_tag_option_id"] == "opt-101"

        bill_res = await zoho.create_draft_bill(
            supplier_name="Bikaner Timber",
            invoice_no="INV-77",
            bill_date="2026-11-16",
            line_items=[{"description": "Teak Ply", "rate": 45000, "quantity": 1}],
            vendor_id="zv-1",
            reference_number="MUNIM-G1-S1",
            project_id=pid,
            sb=sb,
        )
        assert bill_res["bill"]["bill_id"] == "zb-new"
        assert bill_res["bill"]["status"] == "draft"

        dup_res = await zoho.create_draft_bill(
            supplier_name="Bikaner Timber",
            invoice_no="INV-77",
            bill_date="2026-11-16",
            line_items=[{"description": "Teak Ply", "rate": 45000, "quantity": 1}],
            vendor_id="zv-1",
            reference_number="MUNIM-DUP-S0",
            project_id=pid,
            sb=sb,
        )
        assert dup_res["deduped"] is True
        assert dup_res["bill"]["bill_id"] == "zb-existing"

        sync_res = await zoho.sync_project_financial_caches(sb)
        assert sync_res["synced"] == 1

    prj_after = sb.store["projects"][0]
    assert prj_after["zoho_tag_id"] == "tag-prj"
    assert prj_after["zoho_tag_option_id"] == "opt-101"
    assert prj_after["zoho_invoiced_cache"] == 120000.0
    assert prj_after["zoho_bills_cache"] == 45000.0


def test_pass1_sheets_backup_v1_tabs_and_error_resilience():
    assert "item_parts" not in sheets_backup.BACKUP_TABS
    assert "cash_ledger" not in sheets_backup.BACKUP_TABS
    assert "followups" not in sheets_backup.BACKUP_TABS
    for required_tab in (
        "customers",
        "vendors",
        "people",
        "payment_accounts",
        "projects",
        "project_items",
        "item_materials",
        "stage_events",
        "agent_goals",
        "v_project_pnl",
        "v_whos_doing_what",
        "v_master_purchase_list",
    ):
        assert required_tab in sheets_backup.BACKUP_TABS

    class FailingTabSupabase(FakeSupabase):
        def table(self, name: str):
            if name == "v_master_purchase_list":
                raise RuntimeError("view timeout")
            return super().table(name)

    sb = FailingTabSupabase({"customers": [{"id": "c1", "full_name": "Praveen"}]})
    fake_settings = Settings(backup_sheet_id="sheet-123")
    mock_service = MagicMock()
    mock_sheets_api = mock_service.spreadsheets.return_value
    mock_sheets_api.get.return_value.execute.return_value = {"sheets": []}
    with (
        patch("app.services.sheets_backup.get_settings", return_value=fake_settings),
        patch("app.services.sheets_backup.google.auth.default", return_value=(MagicMock(), "proj")),
        patch("app.services.sheets_backup.build", return_value=mock_service),
    ):
        out = sheets_backup.export_readonly_sheet_snapshot(sb)

    assert out["exported"] is True
    assert out["row_counts"]["customers"] == 1
    assert out["row_counts"]["v_master_purchase_list"] == -1
    assert out["row_counts"]["projects"] == 0


@pytest.mark.asyncio
async def test_pass1_telegram_stale_claim_recovery_and_multi_voice_burst():
    now_utc = datetime.now(timezone.utc)
    sb = FakeSupabase(
        {
            "people": [
                {
                    "id": "p-owner",
                    "full_name": "Shreyansh",
                    "telegram_user_id": 1001,
                    "is_active": True,
                }
            ],
            "telegram_inbox_buffer": [
                {
                    "id": "buf-stale",
                    "telegram_update_id": 101,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "production",
                    "status": "claimed",
                    "claimed_at": (now_utc - timedelta(seconds=180)).isoformat(),
                    "created_at": (now_utc - timedelta(seconds=200)).isoformat(),
                    "raw_payload": {
                        "message": {
                            "message_id": 11,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "voice": {"file_id": "voice-1"},
                        }
                    },
                },
                {
                    "id": "buf-fresh",
                    "telegram_update_id": 102,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "production",
                    "status": "buffered",
                    "created_at": (now_utc - timedelta(seconds=10)).isoformat(),
                    "raw_payload": {
                        "message": {
                            "message_id": 12,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "voice": {"file_id": "voice-2"},
                            "caption": "Also check kitchen",
                        }
                    },
                },
            ],
            "agent_goals": [],
        }
    )

    captured: dict[str, Any] = {}

    async def fake_download(fid: str):
        return f"audio-{fid}".encode("utf-8"), "audio/ogg"

    async def fake_run_turn(sb_arg, **kwargs):
        captured.update(kwargs)
        return {"kind": "direct_reply", "heard_transcript": "Clip1 + Clip2", "reply_text": "All noted.", "goals": []}

    fake_settings = Settings(telegram_burst_debounce_seconds=2.5)
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=fake_settings),
        patch("app.routers.telegram.telegram_api.download_file_bytes", side_effect=fake_download),
        patch("app.routers.telegram.telegram_api.send_message", new=AsyncMock(return_value={"result": {"message_id": 99}})),
        patch("app.routers.telegram.agent_core.run_agent_turn", side_effect=fake_run_turn),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert captured["audio_bytes"] == b"audio-voice-1"
    assert captured["extra_audio_files"] == [(b"audio-voice-2", "audio/ogg")]
    assert "Also check kitchen" in (captured["text"] or "")
    assert all(r["status"] == "processed" for r in sb.store["telegram_inbox_buffer"])


@pytest.mark.asyncio
async def test_four_cash_wallets_natural_shorthand_and_zoho_safe_matching():
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-galla",
                    "code": "cash_galla",
                    "display_name": "Cash Galla",
                    "account_kind": "cash_galla",
                    "aliases": ["cash", "galla", "shop cash", "nakad"],
                    "sort_order": 1,
                    "is_active": True,
                },
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis", "axis bank"],
                    "sort_order": 2,
                    "is_active": True,
                },
                {
                    "id": "acc-rgb",
                    "code": "rgb",
                    "display_name": "RGB (Gramin Bank)",
                    "account_kind": "bank",
                    "aliases": ["rgb", "rajasthan gramin bank"],
                    "sort_order": 3,
                    "is_active": True,
                },
                {
                    "id": "acc-ccrgb",
                    "code": "ccrgb",
                    "display_name": "CCRGB (OD / CC)",
                    "account_kind": "od_cc",
                    "aliases": ["ccrgb", "cc rgb", "od"],
                    "sort_order": 4,
                    "is_active": True,
                },
                {
                    "id": "acc-ansh",
                    "code": "float_ansh",
                    "display_name": "Ansh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["ansh", "ansh wallet", "ansh float", "ansh cash", "ansh ji", "ansh bhaiya"],
                    "sort_order": 10,
                    "is_active": True,
                },
                {
                    "id": "acc-kamlesh",
                    "code": "float_kamlesh",
                    "display_name": "Kamlesh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["kamlesh", "kamlesh wallet", "kamlesh float", "kamlesh cash", "kamlesh bhaiya", "kamlesh ji"],
                    "sort_order": 11,
                    "is_active": True,
                },
                {
                    "id": "acc-rohit",
                    "code": "float_rohit",
                    "display_name": "Rohit Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["rohit", "rohit wallet", "rohit float", "rohit cash", "rohit ji", "rohit bhaiya"],
                    "sort_order": 12,
                    "is_active": True,
                },
                {
                    "id": "acc-shreyansh",
                    "code": "float_shreyansh",
                    "display_name": "Shreyansh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["shreyansh", "shreyansh wallet", "shreyansh float", "shreyansh cash", "shreyansh ji", "owner wallet"],
                    "sort_order": 13,
                    "is_active": True,
                },
            ]
        }
    )

    # 3-tier priority natural shorthand checks
    assert agent_core._extract_payment_account_code_from_text(sb, "Ansh paid 500 for screws") == "float_ansh"
    assert agent_core._extract_payment_account_code_from_text(sb, "Rohit paid 300 cash for auto") == "float_rohit"
    assert agent_core._extract_payment_account_code_from_text(sb, "Ansh paid 500 from galla") == "cash_galla"
    assert agent_core._extract_payment_account_code_from_text(sb, "Shreyansh paid 5,000 from Axis") == "axis"
    assert agent_core._extract_payment_account_code_from_text(sb, "Praveen Mehta advance 20,000 cash") == "cash_galla"

    # Word-bounded & account-type-safe Zoho /bankaccounts sync (note CCRGB before RGB, and SBI - Shreyansh distractor)
    fake_zoho_accs = [
        {"account_id": "z-undep", "account_name": "Undeposited Funds", "account_type": "cash", "is_active": True, "balance": -6000},
        {"account_id": "z-petty", "account_name": "Petty Cash", "account_type": "cash", "is_active": True, "balance": 100},
        {"account_id": "z-galla", "account_name": "Cash Galla", "account_type": "cash", "is_active": True, "balance": 12500},
        {"account_id": "z-ccrgb", "account_name": "CCRGB", "account_type": "bank", "is_active": True, "balance": -50000},
        {"account_id": "z-rgb", "account_name": "RGB", "account_type": "bank", "is_active": True, "balance": 42000},
        {"account_id": "z-axis", "account_name": "Axis Bank", "account_type": "bank", "is_active": True, "balance": 85000},
        {"account_id": "z-sbi-shr", "account_name": "SBI - Shreyansh", "account_type": "bank", "is_active": True, "balance": 99999},
        {"account_id": "z-ansh", "account_name": "Ansh Wallet", "account_type": "cash", "is_active": True, "balance": 4500},
        {"account_id": "z-kam", "account_name": "Kamlesh Wallet", "account_type": "cash", "is_active": True, "balance": 3000},
        {"account_id": "z-roh", "account_name": "Rohit Wallet", "account_type": "cash", "is_active": True, "balance": 2000},
        {"account_id": "z-shr", "account_name": "Shreyansh Wallet", "account_type": "cash", "is_active": True, "balance": 15000},
    ]

    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
    )

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        assert method == "GET" and path == "/bankaccounts"
        return httpx.Response(200, json={"bankaccounts": fake_zoho_accs})

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        sync_out = await zoho.sync_owner_pulse_snapshot(sb)

    assert sync_out["ok"] is True
    assert sync_out["accounts_synced"] == 8
    by_code = {a["code"]: a for a in sb.store["payment_accounts"]}
    assert by_code["cash_galla"]["zoho_account_id"] == "z-galla"
    assert by_code["axis"]["zoho_account_id"] == "z-axis"
    assert by_code["rgb"]["zoho_account_id"] == "z-rgb"
    assert by_code["ccrgb"]["zoho_account_id"] == "z-ccrgb"
    assert by_code["float_ansh"]["zoho_account_id"] == "z-ansh"
    assert by_code["float_kamlesh"]["zoho_account_id"] == "z-kam"
    assert by_code["float_rohit"]["zoho_account_id"] == "z-roh"
    assert by_code["float_shreyansh"]["zoho_account_id"] == "z-shr"


@pytest.mark.asyncio
async def test_expense_categorization_salary_routing_and_zoho_coa_selection():
    # 1. Inference & LLM normalization across all workshop categories
    assert goal_primitives.normalize_or_infer_expense_category(None, "Paid 15,000 salary to Lala Ji") == "salary_wages"
    assert goal_primitives.normalize_or_infer_expense_category("Salaries", "") == "salary_wages"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Ansh paid 650 for tempo bhada") == "transport_freight"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Rohit paid 420 for fevicol and screws") == "raw_materials"
    assert goal_primitives.normalize_or_infer_expense_category(None, "200 chai nasta workshop") == "food_tea_mess"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Shop rent 25,000 from RGB") == "rent"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Electricity bijli bill 4500") == "utilities_office"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Cutter blade sharpening 300") == "repairs_maintenance"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Random misc 100") == "other_expense"

    # 2. Salaried-karigar auto-guard flips vendor_payment -> expense(salary_wages), keeps real vendors untouched
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis", "axis bank"],
                    "zoho_account_id": "za-axis",
                    "is_active": True,
                }
            ],
            "people": [
                {
                    "id": "p-lala",
                    "full_name": "Lala Ji",
                    "person_type": "salaried_karigar",
                    "aliases": ["Lala"],
                    "payout_aliases": ["Ruksar Bano"],
                    "is_active": True,
                },
                {
                    "id": "p-manish",
                    "full_name": "Manish Cushioning",
                    "person_type": "contractor_mistri",
                    "aliases": ["Manish"],
                    "payout_aliases": [],
                    "is_active": True,
                },
            ],
            "vendors": [
                {
                    "id": "v-doorset",
                    "canonical_name": "Doorset",
                    "vendor_kind": "supplier",
                    "aliases": [],
                    "is_active": True,
                }
            ],
        }
    )

    karigar_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Lala Ji",
        "amount_inr": 15000,
        "payment_account_code": "axis",
        "summary_title": "Paid Lala Ji",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, karigar_slots, user_text="Paid 15,000 to Lala Ji from Axis", heard_transcript=None
    )
    assert karigar_slots["operation_kind"] == "expense"
    assert karigar_slots["expense_category"] == "salary_wages"

    proxy_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Ruksar Bano",
        "amount_inr": 8000,
        "payment_account_code": "axis",
        "summary_title": "Transfer to Ruksar Bano",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, proxy_slots, user_text="Paid 8,000 to Ruksar Bano from Axis", heard_transcript=None
    )
    assert proxy_slots["operation_kind"] == "expense"
    assert proxy_slots["expense_category"] == "salary_wages"

    vendor_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Doorset",
        "amount_inr": 12000,
        "payment_account_code": "axis",
        "summary_title": "Paid Doorset",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, vendor_slots, user_text="Paid 12,000 to Doorset from Axis", heard_transcript=None
    )
    # Cash-first informal rule: routine payouts become immediate Expenses; explicit settlement stays vendor_payment
    assert vendor_slots["operation_kind"] == "expense"

    settle_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Doorset",
        "amount_inr": 12000,
        "payment_account_code": "axis",
        "summary_title": "Settled Doorset pending bill",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, settle_slots, user_text="Settled pending bill 12,000 to Doorset from Axis", heard_transcript=None
    )
    assert settle_slots["operation_kind"] == "vendor_payment"

    # 3. Confirmation card includes human Category line
    confirm_txt = agent_core._format_confirmation_summary("expense", karigar_slots)
    assert "Category: *Salaries & Wages*" in confirm_txt

    # 4. Zoho push selects exact Chart of Accounts account_id (never blindly index 0)
    assert goal_primitives.normalize_or_infer_expense_category("cogs", "") == "raw_materials"
    sb.store["customers"] = [
        {"id": "c-arjun", "full_name": "Arjun", "zoho_contact_id": "zc-arjun", "is_active": True}
    ]
    sb.store["projects"] = [
        {
            "id": "prj-arjun",
            "project_code": "PRJ-ARJUN",
            "cf_project_code": "PRJ-ARJUN",
            "customer_id": "c-arjun",
            "customer_name": "Arjun",
            "zoho_contact_id": "zc-arjun",
            "zoho_tag_id": "tag-prj",
            "zoho_tag_option_id": "opt-arjun",
            "status": "active",
            "project_type": "customer",
        }
    ]
    sb.store["entity_dictionary"] = []

    fake_coa = [
        {"account_id": "coa-it", "account_name": "IT and Internet Expenses", "account_type": "expense", "is_active": True},
        {"account_id": "coa-raw-exp", "account_name": "Raw Materials And Consumables", "account_type": "expense", "is_active": True},
        {"account_id": "coa-mat-cogs", "account_name": "Materials", "account_type": "cost_of_goods_sold", "is_active": True},
        {"account_id": "coa-sal", "account_name": "Salaries and Employee Wages", "account_type": "expense", "is_active": True},
        {"account_id": "coa-trn", "account_name": "Transportation Expense", "account_type": "expense", "is_active": True},
        {"account_id": "coa-oth", "account_name": "Other Expenses", "account_type": "expense", "is_active": True},
    ]
    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
    )
    posted_expenses: list[dict[str, Any]] = []
    contact_calls: list[dict[str, Any]] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/expenses":
            return httpx.Response(200, json={"expenses": []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": fake_coa})
        if method == "GET" and path == "/contacts":
            contact_calls.append(dict(params or {}))
            if "lala" in str((params or {}).get("contact_name_contains") or "").lower():
                return httpx.Response(200, json={"contacts": [{"contact_id": "zv-lala", "contact_name": "Lala Ji"}]})
            return httpx.Response(200, json={"contacts": []})
        if method == "POST" and path == "/expenses":
            posted_expenses.append(dict(json_body or {}))
            return httpx.Response(200, json={"expense": {"expense_id": f"ze-{len(posted_expenses)}"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res_sal = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=15000,
            payment_account_id="acc-axis",
            reference_number="MUNIM-SAL-1",
            party_name="Lala Ji",
            notes="October salary",
            expense_category="salary_wages",
        )
        # Proxy alias 'Ruksar Bano' uses cached Lala Ji vendor_id and traces via-alias in description
        res_proxy = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=8000,
            payment_account_id="acc-axis",
            reference_number="MUNIM-SAL-2",
            party_name="Ruksar Bano",
            notes="Salary advance",
            expense_category="salary_wages",
        )
        # Project-linked expense attaches Zoho customer_id; generic payee skips /contacts
        res_trn = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=650,
            payment_account_id="acc-axis",
            reference_number="MUNIM-TRN-1",
            party_name="tempo bhada",
            project_id="prj-arjun",
            notes="tempo bhada",
            expense_category="transport_freight",
        )
        res_oth = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=100,
            payment_account_id="acc-axis",
            reference_number="MUNIM-OTH-1",
            notes="random",
            expense_category="other_expense",
        )
        res_mat = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=420,
            payment_account_id="acc-axis",
            reference_number="MUNIM-MAT-1",
            notes="fevicol and screws",
            expense_category="raw_materials",
        )

    assert [e["account_id"] for e in posted_expenses] == ["coa-sal", "coa-sal", "coa-trn", "coa-oth", "coa-mat-cogs"]
    assert posted_expenses[0]["vendor_id"] == "zv-lala"
    assert posted_expenses[1]["vendor_id"] == "zv-lala"
    assert "(via Ruksar Bano)" in posted_expenses[1]["description"]
    assert "vendor_id" not in posted_expenses[2]
    assert posted_expenses[2]["customer_id"] == "zc-arjun"
    assert "vendor_id" not in posted_expenses[3]
    assert "vendor_id" not in posted_expenses[4]
    assert len(contact_calls) == 1  # cached on Lala Ji's metadata; generic words skipped!
    assert sb.store["people"][0]["metadata"]["zoho_contact_id"] == "zv-lala"
    assert res_sal["expense_account_name"] == "Salaries and Employee Wages"
    assert res_proxy["expense_account_name"] == "Salaries and Employee Wages"
    assert res_trn["expense_account_name"] == "Transportation Expense"
    assert res_oth["expense_account_name"] == "Other Expenses"
    assert res_mat["expense_account_name"] == "Materials"


@pytest.mark.asyncio
async def test_zoho_ledger_reader_endpoint_and_ai_tool():
    from app.auth import CurrentUser, get_current_user
    from app.main import app
    from app.services import agent_tools

    sb = FakeSupabase(
        {
            "people": [
                {
                    "id": "p-shr",
                    "full_name": "Shreyansh",
                    "person_type": "operator",
                    "email": "shr@example.com",
                    "can_login_munim": True,
                    "is_active": True,
                },
                {
                    "id": "p-lala",
                    "full_name": "Lala Ji",
                    "person_type": "salaried_karigar",
                    "aliases": [],
                    "payout_aliases": ["Ruksar Bano"],
                    "is_active": True,
                },
            ],
            "payment_accounts": [
                {
                    "id": "acc-galla",
                    "code": "cash_galla",
                    "display_name": "Cash Galla",
                    "aliases": ["cash", "galla"],
                    "zoho_account_id": "za-galla",
                    "is_active": True,
                }
            ],
            "vendors": [],
            "customers": [],
        }
    )
    fake_settings = Settings(zoho_client_id="cid", zoho_client_secret="csec", zoho_refresh_token="rtok", zoho_org_id="org1")

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        assert method == "GET"
        if path == "/expenses":
            return httpx.Response(
                200,
                json={
                    "expenses": [
                        {
                            "expense_id": "ze-1",
                            "date": "2026-11-17",
                            "created_time": "2026-11-17T02:35:00+0530",
                            "total": 1000.0,
                            "paid_through_account_name": "Cash Galla",
                            "paid_through_account_id": "za-galla",
                            "account_name": "Salaries and Employee Wages",
                            "vendor_name": "Lala Ji",
                            "customer_name": "",
                            "description": "[Rishabh] Lala Ji Salary advance",
                            "reference_number": "MUNIM-1-S0",
                            "status": "nonbillable",
                        }
                    ]
                },
            )
        if path == "/customerpayments":
            return httpx.Response(
                200,
                json={
                    "customerpayments": [
                        {
                            "payment_id": "zp-in-1",
                            "date": "2026-11-16",
                            "created_time": "2026-11-16T18:00:00+0530",
                            "amount": 50000.0,
                            "account_name": "Axis Bank",
                            "account_id": "za-axis",
                            "customer_name": "Arjun (Jaipur)",
                            "description": "Booking advance",
                            "reference_number": "MUNIM-2-S0",
                            "status": "paid",
                        }
                    ]
                },
            )
        if path == "/vendorpayments":
            return httpx.Response(200, json={"vendorpayments": []})
        if path == "/banktransactions":
            return httpx.Response(
                200,
                json={
                    "banktransactions": [
                        # Mirrored expense (same id ze-1) MUST be deduped!
                        {
                            "transaction_id": "ze-1",
                            "transaction_type": "expense",
                            "date": "2026-11-17",
                            "amount": 1000.0,
                            "account_name": "Cash Galla",
                            "account_id": "za-galla",
                        },
                        # Paired transfer_fund legs MUST collapse to 1 row
                        {
                            "transaction_id": "zt-1",
                            "transaction_type": "transfer_fund",
                            "date": "2026-11-15",
                            "created_time": "2026-11-15T10:00:00+0530",
                            "amount": 5000.0,
                            "account_name": "Cash Galla",
                            "offset_account_name": "Ansh Wallet",
                            "description": "Shop float",
                            "reference_number": "MUNIM-XFER-1",
                        },
                        {
                            "transaction_id": "zt-2",
                            "transaction_type": "transfer_fund",
                            "date": "2026-11-15",
                            "created_time": "2026-11-15T10:00:00+0530",
                            "amount": 5000.0,
                            "account_name": "Ansh Wallet",
                            "offset_account_name": "Cash Galla",
                            "description": "Shop float",
                            "reference_number": "MUNIM-XFER-1",
                        },
                    ]
                },
            )
        if path == "/bills":
            return httpx.Response(200, json={"bills": []})
        if path == "/invoices":
            return httpx.Response(200, json={"invoices": []})
        raise AssertionError(f"Unexpected path: {path}")

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="google", identifier="shr@example.com", display_name="Shreyansh"
    )
    try:
        with (
            patch("app.services.zoho.get_settings", return_value=fake_settings),
            patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
            patch("app.routers.domains.get_supabase_admin", return_value=sb),
        ):
            all_rows = await zoho.list_recent_ledger_transactions(sb, limit=20)
            assert len(all_rows) == 3  # ze-1 (not duplicated!), zp-in-1, 1 float_transfer
            assert [r["id"] for r in all_rows] == ["ze-1", "zp-in-1", "zt-1"]

            tool_json = await agent_tools.execute_read_tool(
                sb, "list_recent_transactions", {"party_or_search": "Ruksar Bano"}
            )
            tool_data = json.loads(tool_json)
            assert len(tool_data["transactions"]) == 1
            assert tool_data["transactions"][0]["party_name"] == "Lala Ji"

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get("/api/munim/ledger?account=Cash+Galla")
                assert r.status_code == 200
                txns = r.json()["transactions"]
                assert [t["id"] for t in txns] == ["ze-1", "zt-1"]
    finally:
        app.dependency_overrides.clear()
