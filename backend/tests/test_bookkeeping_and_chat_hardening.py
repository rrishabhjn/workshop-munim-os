"""Tests for cash-first informal bookkeeping (Sales, Expenses, Draft AR/AP) and AI chat hardening (Rec #1-#3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.routers import telegram as tg_router
from app.services import agent_core, attachment_memory, storage, zoho
from conftest import seed_goal_intelligence_sb


@pytest.mark.asyncio
async def test_sales_receipts_walkin_counter_and_active_project_payment_track():
    """Walk-in counter sales book Zoho /banktransactions -> Sales without project_id; active orders update Payment Track."""
    sb = seed_goal_intelligence_sb()
    sb.store["customers"].append(
        {"id": "c-chiranji", "full_name": "Chiranji Lal", "is_active": True}
    )
    sb.store["payment_accounts"].append(
        {
            "id": "acc-rgb",
            "code": "rgb",
            "display_name": "RGB (Gramin Bank)",
            "account_kind": "bank",
            "zoho_account_id": "za-rgb",
            "aliases": ["rgb"],
            "is_active": True,
        }
    )
    sb.store["payment_accounts"][0]["zoho_account_id"] = "za-cash"
    sb.store["projects"][0]["payment_stage"] = "advance_pending"
    sb.store["projects"][0]["paid_cache_inr"] = 0.0
    sb.store["projects"][0]["zoho_contact_id"] = "zc-amb"
    sb.store["entity_dictionary"] = []

    posted_bt: list[dict[str, Any]] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/banktransactions":
            return httpx.Response(200, json={"banktransactions": []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(
                200,
                json={
                    "chartofaccounts": [
                        {"account_id": "inc-gen", "account_name": "General Income", "account_type": "income", "is_active": True},
                        {"account_id": "inc-sales", "account_name": "Sales", "account_type": "income", "is_active": True},
                    ]
                },
            )
        if method == "POST" and path == "/banktransactions":
            body = dict(json_body or {})
            if len(posted_bt) >= 1 and body.get("transaction_type") == "sales_without_invoices":
                return httpx.Response(400, json={"code": 1002, "message": "Invalid transaction_type"})
            posted_bt.append(body)
            return httpx.Response(200, json={"banktransaction": {"transaction_id": f"bt-{len(posted_bt)}"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    # 1. Counter sale ("1,05,000 cash sales to chiranji lal for door and laminate sales") -> no project_id, direct Sales
    t_counter1 = SimpleNamespace(
        id="int_cs1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fcs1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "customer_receipt",
                    "amount_inr": 105000,
                    "party_name": "Chiranji Lal",
                    "payment_account_code": "cash_galla",
                    "notes": "Door and laminate sales",
                },
            )
        ],
        output_text=None,
    )
    t_counter2 = SimpleNamespace(id="int_cs2", steps=[], output_text="")
    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t_counter1, t_counter2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out_counter = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="1,05,000 cash sales to chiranji lal for door and laminate sales",
        )

    assert len(out_counter["goals"]) == 1
    g_counter = out_counter["goals"][0]
    assert g_counter["status"] == "awaiting_confirmation"
    assert not g_counter["collected_slots"].get("project_id")
    assert "Zoho Sales" in g_counter["prompt"]

    fake_settings = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
        patch("app.services.zoho.ensure_project_zoho_bridge", AsyncMock(return_value={"zoho_contact_id": "zc-amb"})),
    ):
        exec_counter = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=g_counter["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        assert exec_counter["status"] == "completed"
        assert "Zoho `sales_deposit`" in exec_counter["receipt"]
        assert "customer_id" not in posted_bt[0]
        assert "Chiranji Lal" in posted_bt[0]["description"]
        assert posted_bt[0]["from_account_id"] == "inc-sales"
        assert posted_bt[0]["to_account_id"] == "za-cash"
        assert posted_bt[0]["amount"] == 105000.0
        assert posted_bt[0]["transaction_type"] == "sales_without_invoices"
        assert len(sb.store["projects"]) == 1  # Never creates a fake project!

        # 2. Active project receipt pushes /banktransactions AND advances Payment Track
        res_proj = await agent_core.step_executors.execute_munim_financial_step(
            sb,
            goal_id="g-amb-50k",
            step_index=0,
            entry_kind="customer_receipt",
            amount_inr=50000.0,
            payment_account_id="acc-rgb",
            party_name="Ambure Ji (Jaipur)",
            project_id="prj-ambure",
            notes="Booking advance",
        )
    assert res_proj["ok"] is True
    assert sb.store["projects"][0]["payment_stage"] == "advance_in"
    assert sb.store["projects"][0]["paid_cache_inr"] == 50000.0
    assert posted_bt[1]["customer_id"] == "zc-amb"
    assert posted_bt[1]["from_account_id"] == "inc-sales"
    assert posted_bt[1]["to_account_id"] == "za-rgb"
    assert posted_bt[1]["transaction_type"] == "other_income"


@pytest.mark.asyncio
async def test_cash_first_expenses_vs_explicit_settlement_and_draft_invoice():
    """Routine payouts normalize to Expense (notes preserved); explicit AR becomes Zoho Draft Invoice."""
    sb = seed_goal_intelligence_sb()
    sb.store["vendors"].append(
        {"id": "v-suresh", "canonical_name": "Suresh Ji", "vendor_kind": "contractor", "specialty": "Painter", "aliases": [], "is_active": True}
    )
    sb.store["people"].append(
        {"id": "p-rajesh", "full_name": "Rajesh Ji", "person_type": "salaried_karigar", "aliases": [], "is_active": True}
    )

    # 1. "5000 cash paid to suresh ji painter" staged as vendor_payment -> normalized to expense(contractor_labor)
    t1 = SimpleNamespace(
        id="int_p1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fp1",
                name="stage_financial_entry",
                arguments={"entry_kind": "vendor_payment", "amount_inr": 5000, "party_name": "suresh ji painter", "payment_account_code": "cash_galla"},
            )
        ],
        output_text=None,
    )
    t2 = SimpleNamespace(id="int_p2", steps=[], output_text="")
    # 2. Explicit AR receivable ("Ambure Ji owes us 25,000 for extra wardrobe") -> Draft Invoice
    t3 = SimpleNamespace(
        id="int_ar1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="far1",
                name="stage_financial_entry",
                arguments={"entry_kind": "customer_draft_invoice", "amount_inr": 25000, "party_name": "Ambure Ji", "notes": "Extra wardrobe balance"},
            )
        ],
        output_text=None,
    )
    t4 = SimpleNamespace(id="int_ar2", steps=[], output_text="")
    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t1, t2, t3, t4]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out_exp = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="5000 cash paid to suresh ji painter",
        )
        out_ar = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Ambure Ji owes us 25,000 for extra wardrobe",
        )

    s_exp = out_exp["goals"][0]["collected_slots"]
    assert s_exp["operation_kind"] == "expense"
    assert s_exp["expense_category"] == "contractor_labor"
    assert s_exp["party_name"] == "Suresh Ji"
    assert "PAISA GAYA" in out_exp["goals"][0]["prompt"]

    g_ar = out_ar["goals"][0]
    assert g_ar["collected_slots"]["operation_kind"] == "customer_draft_invoice"
    assert "Confirm Zoho Draft Invoice" in g_ar["prompt"]

    posted_inv: list[dict[str, Any]] = []

    async def fake_inv_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/invoices":
            return httpx.Response(200, json={"invoices": []})
        if method == "POST" and path == "/invoices":
            posted_inv.append(dict(json_body or {}))
            return httpx.Response(200, json={"invoice": {"invoice_id": "zinv-1", "status": "draft"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    fake_settings = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_inv_req),
        patch("app.services.zoho.ensure_project_zoho_bridge", AsyncMock(return_value={"zoho_contact_id": "zc-amb"})),
    ):
        inv_res = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=g_ar["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
    assert inv_res["status"] == "completed"
    assert posted_inv[0]["status"] == "draft"
    assert posted_inv[0]["customer_id"] == "zc-amb"


@pytest.mark.asyncio
async def test_rec2_captionless_hold_split_pdf_and_upsert_saves_spec_pdf():
    """Captionless PDF holds politely, auto-attaches 30s later, and item upsert saves Spec PDF on order."""
    storage._LOCAL_MEM_BLOBS.clear()
    sb = seed_goal_intelligence_sb()
    chat_id = -1003981258923
    pdf_bytes = b"%PDF-1.4\n1 0 obj << /Title (Arjun Wardrobe) >> endobj"

    # 1. Bare PDF arrives in Production without text -> immediate polite hold card
    with patch(
        "app.services.decisions_triage.triage_media_upload",
        return_value=SimpleNamespace(media_kind="sizing_sheet", ocr_difficulty=0.4, recommended_thinking="low", confidence=0.9),
    ):
        hold_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            text=None,
            media_files=[(pdf_bytes, "application/pdf")],
            telegram_chat_id=chat_id,
        )

    assert len(hold_out["goals"]) == 1
    assert hold_out["goals"][0]["prompt"] == attachment_memory.CAPTIONLESS_HOLD_PROMPT
    hold_id = hold_out["goals"][0]["goal_id"]
    sb.table("agent_goals").update({"telegram_bot_msg_id": 77}).eq("id", hold_id).execute()

    # 2. 30s later user sends "add item to Ambure order, above pdf has details" -> restores PDF, retires bot msg 77, saves Spec PDF
    t_up1 = SimpleNamespace(
        id="int_pdf_follow",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fpdf1",
                name="upsert_order_items",
                arguments={"project_id": "prj-ambure", "items": [{"item_name": "Study Table", "dimensions": "4ft x 2ft"}]},
            )
        ],
        output_text=None,
    )
    t_up2 = SimpleNamespace(id="int_pdf_follow2", steps=[], output_text="")
    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t_up1, t_up2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
    ):
        follow_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            text="add Study Table for Ambure, above pdf has details",
            telegram_chat_id=chat_id,
        )

    assert follow_out["retired_bot_msg_ids"] == [77]
    # Gemini literally received the PDF document block in turn 2
    first_input_blocks = fake_c.interactions.create.call_args_list[0].kwargs["input"]
    assert any(b.get("type") == "document" and b.get("mime_type") == "application/pdf" for b in first_input_blocks)
    # Order metadata stored uploaded_gcs_paths so OrderDetailDrawer renders Spec PDF!
    saved_paths = (sb.store["projects"][0].get("metadata") or {}).get("uploaded_gcs_paths") or []
    assert len(saved_paths) == 1 and saved_paths[0].endswith(".pdf")


@pytest.mark.asyncio
async def test_rec3_in_place_edit_awaiting_confirmation_and_retires_old_telegram_card():
    """Editing an awaiting_confirmation goal in chat updates in-place & strips old Confirm buttons."""
    sb = seed_goal_intelligence_sb()
    sb.store["payment_accounts"].append(
        {"id": "acc-rgb", "code": "rgb", "display_name": "RGB Bank", "account_kind": "bank", "aliases": ["rgb"], "is_active": True}
    )
    chat_id = -1004481937487
    now_utc = datetime.now(timezone.utc)
    sb.store["agent_goals"].append(
        {
            "id": "goal-confirm-axis",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "munim",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 105,
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Expense: Suresh Ji",
            "missing_slots": [],
            "pending_prompt": "🔴 PAISA GAYA (OUT): *₹5,000*\n• Account: `AXIS`",
            "collected_slots": {
                "operation_kind": "expense",
                "amount_inr": 5000.0,
                "party_name": "Suresh Ji",
                "payment_account_code": "axis",
                "payment_account_id": "acc-axis",
                "expense_category": "contractor_labor",
            },
            "steps": [],
            "updated_at": (now_utc - timedelta(minutes=1)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=1)).isoformat(),
        }
    )
    sb.store["telegram_inbox_buffer"] = [
        {
            "id": "buf-fix-acc",
            "telegram_update_id": 99001,
            "chat_id": chat_id,
            "user_id": 1001,
            "domain_channel": "munim",
            "message_id": 106,
            "reply_to_message_id": 105,
            "status": "buffered",
            "created_at": (now_utc - timedelta(seconds=5)).isoformat(),
            "raw_payload": {
                "update_id": 99001,
                "message": {
                    "message_id": 106,
                    "chat": {"id": chat_id, "type": "supergroup"},
                    "from": {"id": 1001, "first_name": "Shreyansh"},
                    "reply_to_message": {"message_id": 105, "from": {"is_bot": True}},
                    "text": "Nahi, Axis se nahi RGB se",
                },
            },
        }
    ]

    t1 = SimpleNamespace(
        id="int_r1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fr1",
                name="resolve_open_goal",
                arguments={"goal_id": "goal-confirm-axis", "payment_account_code": "rgb"},
            )
        ],
        output_text=None,
    )
    t2 = SimpleNamespace(id="int_r2", steps=[], output_text="")
    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t1, t2]
    mock_edit = AsyncMock(return_value={"ok": True})
    mock_send = AsyncMock(return_value={"ok": True, "result": {"message_id": 107}})

    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
        patch("app.routers.telegram.telegram_api.edit_message_text", mock_edit),
        patch("app.routers.telegram.telegram_api.send_message", mock_send),
    ):
        await tg_router.process_debounced_burst(chat_id, 1001, wait_locally=False)

    assert len(sb.store["agent_goals"]) == 1
    assert sb.store["agent_goals"][0]["collected_slots"]["payment_account_code"] == "rgb"
    assert sb.store["agent_goals"][0]["telegram_bot_msg_id"] == 107
    # Old card 105 had its buttons stripped before posting new card 107!
    mock_edit.assert_awaited_once()
    assert mock_edit.await_args.kwargs["message_id"] == 105
    assert mock_edit.await_args.kwargs["reply_markup"] == {"inline_keyboard": []}
    assert "RGB" in mock_send.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_latency_optimizations_1_to_4():
    """Verify early-exit loop (#1), INR comma filter (#2), customer matcher (#3), and slim Munim grounding (#4)."""
    from app.services.agent_core.grounding import _build_compact_grounding
    from app.services.goal_primitives import has_complex_turn_cues

    # Fix #2: Numeric grouping commas (1,05,000) stay low thinking; real multi-item lists stay medium
    assert has_complex_turn_cues("1,05,000 cash sales to chiranji lal for door and laminates") is False
    assert has_complex_turn_cues("1,05,000, 50,000") is False
    assert has_complex_turn_cues("ply, laminate, fevicol") is True

    sb = seed_goal_intelligence_sb()
    sb.store["customers"].append(
        {
            "id": "c-chir",
            "full_name": "Chiranji Lal",
            "area_landmark": "Beawar",
            "aliases": ["Chiranji"],
            "zoho_contact_id": "zc-chir-9",
            "is_active": True,
        }
    )

    # Fix #4: Munim grounding omits bulky project_items; Production preserves them
    munim_ctx_raw, _ = _build_compact_grounding(
        sb, owner_person_id="p-shreyansh", domain_scope="munim", ui_context=None
    )
    prod_ctx_raw, _ = _build_compact_grounding(
        sb, owner_person_id="p-shreyansh", domain_scope="production", ui_context=None
    )
    import json

    munim_ctx = json.loads(munim_ctx_raw)
    prod_ctx = json.loads(prod_ctx_raw)
    assert "items" not in munim_ctx["active_orders_index"][0]
    assert "items" in prod_ctx["active_orders_index"][0]

    # Fix #1 + #3: Single Gemini round-trip early-exits on staged:true & deterministically matches customer alias
    t_single = SimpleNamespace(
        id="int_fast1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fc_fast1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "customer_receipt",
                    "amount_inr": 105000,
                    "party_name": "Chiranji",
                    "payment_account_code": "cash_galla",
                    "notes": "Door and laminates",
                },
            )
        ],
        output_text=None,
    )
    fake_c = MagicMock()
    fake_c.interactions.create.return_value = t_single

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="1,05,000 cash sales to chiranji for door and laminates",
        )

    assert fake_c.interactions.create.call_count == 1  # Zero wasted Round 2!
    assert fake_c.interactions.create.call_args.kwargs["generation_config"]["thinking_level"] == "low"
    slots = out["goals"][0]["collected_slots"]
    assert slots["party_name"] == "Chiranji Lal"
    assert slots["customer_id"] == "c-chir"
    assert slots["zoho_contact_id"] == "zc-chir-9"
    assert slots["is_new_payee"] is False


@pytest.mark.asyncio
async def test_same_name_customer_vendor_zoho_collision_auto_suffixes():
    """Customer/Vendor sharing a name auto-suffixes on Zoho 3062 collision instead of locking out."""
    fake_settings = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    posted_names: list[tuple[str, str]] = []
    double_collide = False

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "POST" and path == "/contacts":
            cname = str((json_body or {}).get("contact_name") or "")
            ctype = str((json_body or {}).get("contact_type") or "")
            posted_names.append((cname, ctype))
            if cname in ("Suresh Ji", "Sunil"):
                return httpx.Response(
                    400,
                    json={"code": 3062, "message": f"The contact name {cname} already exists."},
                )
            if cname == "Suresh Ji (Customer)":
                if double_collide:
                    return httpx.Response(
                        400,
                        json={"code": 3062, "message": "The contact name Suresh Ji (Customer) already exists."},
                    )
                return httpx.Response(
                    201,
                    json={"contact": {"contact_id": "zc-suresh-cust", "contact_name": "Suresh Ji (Customer)"}},
                )
            if cname == "Sunil (Vendor)":
                return httpx.Response(
                    201,
                    json={"contact": {"contact_id": "zv-sunil-vend", "contact_name": "Sunil (Vendor)"}},
                )
        if method == "GET" and path == "/contacts":
            return httpx.Response(
                200,
                json={
                    "contacts": [
                        {"contact_id": "zc-suresh-other", "contact_name": "Suresh Kumawat"},
                        {"contact_id": "zc-suresh-cust", "contact_name": "Suresh Ji (Customer)"},
                    ]
                },
            )
        raise AssertionError(f"Unexpected Zoho call: {method} {path} params={params}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        # 1. Vendor "Suresh Ji" exists -> Customer "Suresh Ji" auto-suffixes to "Suresh Ji (Customer)"
        res_cust = await zoho.create_zoho_contact("Suresh Ji", "customer")
        assert res_cust["ok"] is True
        assert res_cust["contact"]["contact_id"] == "zc-suresh-cust"
        assert res_cust["contact"]["contact_name"] == "Suresh Ji (Customer)"

        # 2. Customer "Sunil" exists -> Vendor "Sunil" auto-suffixes to "Sunil (Vendor)"
        res_vend = await zoho.create_zoho_contact("Sunil", "vendor")
        assert res_vend["ok"] is True
        assert res_vend["contact"]["contact_id"] == "zv-sunil-vend"
        assert res_vend["contact"]["contact_name"] == "Sunil (Vendor)"

        # 3. If both bare AND suffixed already exist, fallback GET resolves existing suffixed row
        double_collide = True
        res_existing = await zoho.create_zoho_contact("Suresh Ji", "customer")
        assert res_existing["ok"] is True
        assert res_existing["contact"]["contact_id"] == "zc-suresh-cust"

        # 4. search_zoho_contacts recognizes "Suresh Ji (Customer)" as an exact match alone
        found = await zoho.search_zoho_contacts("Suresh Ji", "customer")
        assert len(found) == 1
        assert found[0]["contact_id"] == "zc-suresh-cust"

    assert ("Suresh Ji", "customer") in posted_names
    assert ("Suresh Ji (Customer)", "customer") in posted_names
    assert ("Sunil", "vendor") in posted_names
    assert ("Sunil (Vendor)", "vendor") in posted_names


