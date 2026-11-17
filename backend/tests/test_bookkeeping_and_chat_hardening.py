"""Tests for cash-first informal bookkeeping (Sales, Expenses, Draft AR/AP) and AI chat hardening (Rec #1-#3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.routers import telegram as tg_router
from app.services import agent_core, attachment_memory, step_executors, storage, zoho
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
            if len(posted_bt) >= 1 and body.get("transaction_type") == "deposit":
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
        assert posted_bt[0]["transaction_type"] == "deposit"
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
    assert posted_bt[1]["transaction_type"] == "sales_without_invoices"


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


@pytest.mark.asyncio
async def test_payment_order_link_prompt_explicit_advance_pipeline_and_manual_paid_patch():
    """Orders link via 1-tap buttons, explicit 'advance' moves pre-sizing orders, and manual Paid override is additive."""
    sb = seed_goal_intelligence_sb()
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
    sb.store["projects"][0]["pipeline_stage"] = "quoted_awaiting_advance"
    sb.store["projects"][0]["payment_stage"] = "advance_pending"
    sb.store["projects"][0]["paid_cache_inr"] = 0.0

    # 1. Surname + "advance" even when LLM guessed project_id without PRJ-101 in text -> pauses for 1-tap link!
    t_adv1 = SimpleNamespace(
        id="int_ord_adv1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fc_ord_adv1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "customer_receipt",
                    "amount_inr": 50000,
                    "party_name": "Ambure",
                    "project_id": "prj-ambure",
                    "payment_account_code": "rgb",
                    "notes": "Received advance",
                },
            )
        ],
        output_text=None,
    )
    t_adv2 = SimpleNamespace(id="int_ord_adv2", steps=[], output_text="")

    # 2. Payment WITHOUT "advance" -> still prompts link, updates paid_cache_inr, does NOT move pipeline_stage
    t_pay1 = SimpleNamespace(
        id="int_ord_pay1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fc_ord_pay1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "customer_receipt",
                    "amount_inr": 25000,
                    "party_name": "Ambure",
                    "payment_account_code": "cash_galla",
                    "notes": "Part payment",
                },
            )
        ],
        output_text=None,
    )
    t_pay2 = SimpleNamespace(id="int_ord_pay2", steps=[], output_text="")

    # 3. Walk-in override -> clears project_id & leaves order untouched
    t_walk1 = SimpleNamespace(
        id="int_ord_walk1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="fc_ord_walk1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "customer_receipt",
                    "amount_inr": 10000,
                    "party_name": "Ambure",
                    "payment_account_code": "cash_galla",
                    "notes": "Separate chair purchase",
                },
            )
        ],
        output_text=None,
    )
    t_walk2 = SimpleNamespace(id="int_ord_walk2", steps=[], output_text="")

    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t_adv1, t_adv2, t_pay1, t_pay2, t_walk1, t_walk2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out_adv = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Received 50,000 advance from Ambure in RGB",
        )
        g_adv = out_adv["goals"][0]
        assert g_adv["status"] == "gathering_info"
        assert [c["label"] for c in g_adv["choices"]] == [
            "🔗 Ambure Ji (Jaipur) (#PRJ-41)",
            "🚶 Separate / Walk-in Sale",
        ]

        # Tap 🔗 Ambure Ji (Jaipur) (#PRJ-41) -> shows Order line + Sizing preview on Confirm card
        link_patch = json.loads(g_adv["choices"][0]["slot_patch_json"])
        resumed = await agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=g_adv["goal_id"],
            slot_patch=link_patch,
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
        )
        assert resumed["status"] == "awaiting_confirmation"
        assert "#PRJ-41" in resumed["prompt"]
        assert "Site Sizing / BOQ" in resumed["prompt"]

        # Confirm explicit advance -> updates paid_cache_inr, payment_stage AND pipeline_stage
        exec_adv = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=g_adv["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        assert exec_adv["status"] == "completed"
        assert sb.store["projects"][0]["paid_cache_inr"] == 50000.0
        assert sb.store["projects"][0]["payment_stage"] == "advance_in"
        assert sb.store["projects"][0]["pipeline_stage"] == "site_sizing_boq"

        # Reset order to quoted_awaiting_advance and test a linked payment WITHOUT "advance"
        sb.store["projects"][0]["pipeline_stage"] = "quoted_awaiting_advance"
        out_pay = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="25,000 received from Ambure cash",
        )
        g_pay = out_pay["goals"][0]
        assert g_pay["status"] == "gathering_info"
        await agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=g_pay["goal_id"],
            slot_patch=json.loads(g_pay["choices"][0]["slot_patch_json"]),
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        exec_pay = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=g_pay["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        assert exec_pay["status"] == "completed"
        assert sb.store["projects"][0]["paid_cache_inr"] == 75000.0
        assert sb.store["projects"][0]["payment_stage"] == "partial_paid"
        assert sb.store["projects"][0]["pipeline_stage"] == "quoted_awaiting_advance"  # NOT moved without "advance"

        # Walk-in tap -> leaves order untouched
        out_walk = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="10,000 cash received from Ambure for separate chair",
        )
        g_walk = out_walk["goals"][0]
        res_walk_card = await agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=g_walk["goal_id"],
            slot_patch=json.loads(g_walk["choices"][1]["slot_patch_json"]),
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        assert not res_walk_card["collected_slots"].get("project_id")
        await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=g_walk["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
        assert sb.store["projects"][0]["paid_cache_inr"] == 75000.0

        # Manual Edit Order sets offline base Paid ₹ and future Munim payment adds on top
        upd_res = step_executors.update_order_metadata(
            sb,
            project_id="prj-ambure",
            paid_cache_inr=100000.0,
        )
        assert upd_res["ok"] is True
        assert sb.store["projects"][0]["paid_cache_inr"] == 100000.0
        add_res = await step_executors.execute_munim_financial_step(
            sb,
            goal_id="g-add-50k",
            step_index=0,
            entry_kind="customer_receipt",
            amount_inr=50000.0,
            payment_account_id="acc-rgb",
            party_name="Ambure Ji",
            project_id="prj-ambure",
            notes="Second installment",
        )
        assert add_res["ok"] is True
        assert sb.store["projects"][0]["paid_cache_inr"] == 150000.0


@pytest.mark.asyncio
async def test_surgical_fixes_1_to_4():
    """Verify async callback queue + 401 fast-fail + Munim RBAC + Zoho locks/COA cache + regex tightening."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    import httpx
    from app.auth import CurrentUser, get_current_user
    from app.config import get_settings
    from app.main import app
    from app.services.agent_core.slot_resolvers import _extract_inr_amount_from_text
    from app.services.goal_primitives import claim_goal_for_execution
    from app.services.zoho.sync import _extract_party_from_desc

    sb = seed_goal_intelligence_sb()
    sb.store["agent_goals"] = [
        {"id": "g-stale", "status": "executing", "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(), "domain_scope": "munim", "steps": [], "collected_slots": {}},
        {"id": "g-fresh", "status": "executing", "updated_at": datetime.now(timezone.utc).isoformat(), "domain_scope": "munim", "steps": [], "collected_slots": {}},
        {"id": "g-401", "status": "awaiting_confirmation", "domain_scope": "munim", "summary_title": "401 check", "collected_slots": {"operation_kind": "customer_receipt", "amount_inr": 20000, "payment_account_id": "acc-cash"}, "steps": [{"step_index": 0, "target": "zoho", "operation": "customer_receipt", "status": "pending", "retry_count": 0}]},
    ]
    assert claim_goal_for_execution(sb, "g-fresh") is None and claim_goal_for_execution(sb, "g-stale")["id"] == "g-stale"
    fake_hy, dispatched = MagicMock(), []
    with patch("app.services.step_executors.execute_munim_financial_step", AsyncMock(return_value={"ok": False, "status_code": 401, "error_kind": "auth_401", "error": {"code": 57, "message": "Not authorized"}})), patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")), patch("app.services.agent_core._get_interactions_client", return_value=fake_hy):
        err_out = await agent_core.confirm_and_execute_goal(sb, goal_id="g-401", actor_person_id="p-shreyansh", actor_label="Shreyansh")
    assert err_out["status"] == "needs_error_fix" and err_out["choices"] == [] and fake_hy.interactions.create.call_count == 0

    sb.store["people"].append({"id": "p-ops", "full_name": "OpsOnly", "email": "opsonly@example.com", "person_type": "operator", "can_login_munim": False, "is_active": True})
    sb.store["v_whos_doing_what"], sb.store["v_project_pnl"] = [], []
    sb.store["v_needs_info_queue"] = [
        {"goal_id": "g-401", "domain_scope": "munim", "summary_title": "Secret Cash", "status": "needs_error_fix"},
        {"goal_id": "g-prod", "domain_scope": "production", "summary_title": "Prod Lead", "status": "gathering_info"},
    ]
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production", telegram_webhook_secret="sec-t", allowed_telegram_user_ids="1001",
        gcp_project_id="p", gcp_region="r", app_base_url="https://x.run.app", cron_auth_secret="cron-s",
        telegram_bot_token="bot-tok", telegram_production_chat_id="-1001", telegram_munim_chat_id="-1002",
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(auth_type="supabase", identifier="opsonly@example.com", display_name="Ops")
    mock_cb, sent_briefs = AsyncMock(return_value={"ok": True}), []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("app.routers.telegram.get_supabase_admin", return_value=sb), patch("app.routers.telegram.telegram_api.answer_callback_query", AsyncMock()), patch("app.routers.telegram._dispatch_to_cloud_tasks", side_effect=lambda p, s, delay_seconds=0.0: dispatched.append((p, delay_seconds))), patch("app.routers.telegram._handle_agent_callback", mock_cb), patch("app.routers.domains.get_supabase_admin", return_value=sb), patch("app.routers.cron.get_supabase_admin", return_value=sb), patch("app.routers.cron.telegram_api.send_message", AsyncMock(side_effect=lambda **kw: sent_briefs.append(kw))):
                wh = await client.post("/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "sec-t"}, json={"update_id": 771, "callback_query": {"id": "cq1", "from": {"id": 1001}, "message": {"message_id": 9, "chat": {"id": -1}}, "data": "ag_confirm:g-401"}})
                assert wh.json() == {"ok": True, "queued": True} and "callback_query" in dispatched[0][0]
                assert (await client.post("/tasks/telegram-worker", headers={"X-Cron-Secret": "cron-s"}, json=dispatched[0][0])).status_code == 200 and mock_cb.await_args.kwargs.get("already_answered") is True
                assert (await client.post("/api/munim/people", json={"full_name": "Hacker", "person_type": "operator"})).status_code == 403
                assert (await client.post("/api/munim/people", json={"full_name": "Proxy", "payout_aliases": ["Alias"]})).status_code == 403
                assert (await client.post("/api/agent/goals/g-401/confirm")).status_code == 403
                assert [r["goal_id"] for r in (await client.get("/api/agent/needs-info")).json()] == ["g-prod"]
                assert [r["goal_id"] for r in (await client.get("/api/production/overview")).json()["needs_info"]] == ["g-prod"]
                assert (await client.post("/cron/morning-brief", headers={"X-Cron-Secret": "cron-s"})).status_code == 200
                assert "Secret Cash" not in sent_briefs[0]["text"] and "Secret Cash" in sent_briefs[1]["text"]
    finally:
        app.dependency_overrides.clear()

    zoho._token_cache.update({"access_token": None, "expires_at": 0.0})
    zoho._COA_CACHE.clear()
    tok_posts, coa_gets = [0], [0]
    async def fake_post(self, url, **kw):
        tok_posts[0] += 1; await asyncio.sleep(0.02); return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "tok-1", "expires_in": 3600})
    async def fake_coa_req(c, m, p, **kw):
        coa_gets[0] += 1; return httpx.Response(200, json={"chartofaccounts": [{"account_id": "a1", "account_name": "Sales", "account_type": "income"}]})
    with patch("app.services.zoho.get_settings", return_value=Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")), patch("httpx.AsyncClient.post", new=fake_post), patch("app.services.zoho._zoho_request", side_effect=fake_coa_req):
        assert len(set(await asyncio.gather(*[zoho._get_access_token() for _ in range(6)]))) == 1 and tok_posts[0] == 1
        async with httpx.AsyncClient() as hc:
            assert len(await zoho._get_chart_of_accounts(hc)) == 1 and len(await zoho._get_chart_of_accounts(hc)) == 1
        assert coa_gets[0] == 1
    assert _extract_party_from_desc("[Rishabh] [PRJ-01] Chiranji Lal — Door") == "Chiranji Lal"

    assert all(_extract_inr_amount_from_text(t) is None for t in ("18mm ply", "10ft bed", "25 sheets", "Room 12", "Sector 140"))
    assert _extract_inr_amount_from_text("₹25") == 25.0 and _extract_inr_amount_from_text("Received 20,000 from Arjun") == 20000.0
    fake_nr = MagicMock()
    fake_nr.interactions.create.side_effect = [
        SimpleNamespace(id="int_no_rep", steps=[SimpleNamespace(type="function_call", id="fc_nr", name="stage_financial_entry", arguments={"entry_kind": "expense", "amount_inr": 5000, "party_name": "Suresh Ji", "notes": "Long description exceeding forty chars without account"})], output_text=None),
        SimpleNamespace(id="int_nr2", steps=[], output_text=""),
    ]
    with patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")), patch("app.services.agent_core._get_interactions_client", return_value=fake_nr):
        nr_out = await agent_core.run_agent_turn(sb, owner_person_id="p-shreyansh", actor_label="Shreyansh", source_channel="telegram", domain_scope="munim", text="5000 paid to Suresh Ji painter for long wall polish work today")
    assert fake_nr.interactions.create.call_count == 2 and all(c.kwargs["generation_config"]["thinking_level"] == "low" for c in fake_nr.interactions.create.call_args_list) and nr_out["goals"][0]["status"] == "gathering_info"



