"""Comprehensive tests for Workshop Inventory & Procurement (/inventory), Zoho asset/FIFO/tag sync, and AI tools."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.auth import CurrentUser, get_current_user
from app.config import Settings
from app.main import app
from app.services import action_tools, agent_core, agent_tools, draft_cart, step_executors, zoho
from conftest import FakeSupabase

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_inv_sb() -> FakeSupabase:
    return FakeSupabase(
        {
            "material_categories": [
                {"code": "ply_wood", "label": "Ply / Wood", "sort_order": 1},
                {"code": "laminate", "label": "Laminate / Sunmica", "sort_order": 2},
                {"code": "hardware", "label": "Hardware", "sort_order": 3},
                {"code": "foam_fabric", "label": "Foam / Fabric", "sort_order": 4},
                {"code": "polish", "label": "Polish / Adhesive", "sort_order": 5},
            ],
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
                    "id": "p-clerk",
                    "full_name": "Ramesh Clerk",
                    "person_type": "operator",
                    "email": "clerk@example.com",
                    "can_login_munim": False,
                    "is_active": True,
                },
            ],
            "vendors": [
                {
                    "id": "v-patel",
                    "canonical_name": "Patel Plywood",
                    "vendor_kind": "supplier",
                    "aliases": ["Patel"],
                    "zoho_contact_id": "zv-patel",
                    "is_active": True,
                }
            ],
            "payment_accounts": [
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis"],
                    "zoho_account_id": "za-axis",
                    "is_active": True,
                }
            ],
            "customers": [{"id": "c-1", "full_name": "Ambure Ji", "is_active": True}],
            "projects": [
                {
                    "id": "prj-1",
                    "project_code": "PRJ-101",
                    "cf_project_code": "PRJ-101",
                    "customer_id": "c-1",
                    "customer_name": "Ambure Ji",
                    "status": "active",
                    "project_type": "customer",
                    "pipeline_stage": "execution",
                    "material_stage": "ordered_pending",
                    "cost_cache_inr": 0.0,
                    "zoho_contact_id": "zc-amb",
                    "zoho_tag_id": "tag-prj",
                    "zoho_tag_option_id": "opt-amb",
                }
            ],
            "project_items": [
                {"id": "it-kitchen", "project_id": "prj-1", "item_name": "Modular Kitchen", "stage": "in_shop"},
            ],
            "item_materials": [],
            "inventory_items": [],
            "inventory_movements": [],
            "stage_events": [],
            "agent_goals": [],
            "entity_dictionary": [],
        }
    )


def test_weighted_average_zero_out_and_hinglish_matching():
    # Moving weighted average math & exact ₹0.00 remainder clear when qty hits 0
    q1, avg1, tot1 = step_executors.compute_weighted_avg_load(10, 100.0, 5, 130.0)
    assert (q1, avg1, tot1) == (15.0, 110.0, 650.0)

    rem_q, rem_avg, issue_cost = step_executors.compute_issue_cost_and_remaining(3, 33.33, 3)
    assert (rem_q, rem_avg, issue_cost) == (0.0, 0.0, 99.99)

    rem_q2, rem_avg2, issue2 = step_executors.compute_issue_cost_and_remaining(15, 110.0, 4)
    assert (rem_q2, rem_avg2, issue2) == (11.0, 110.0, 440.0)

    # Empty catalog on fresh install
    sb = _seed_inv_sb()
    ov = step_executors.get_inventory_overview_data(sb)
    assert ov["items"] == []

    # Hinglish synonyms (<1ms deterministic layer)
    sku_res = step_executors.upsert_inventory_item(
        sb,
        name="18mm Walnut Sunmica Laminate",
        category="laminate",
        unit="sheets",
        min_stock_qty=5,
        aliases=["walnut mica"],
    )
    assert sku_res["ok"] is True
    hit = step_executors.match_inventory_sku(sb, "walnut mica 18mm sheet")
    assert hit is not None and hit["id"] == sku_res["item"]["id"]

    sb.store["item_materials"].append({
        "id": "im-hinge",
        "project_id": "prj-1",
        "item_id": "it-kitchen",
        "material_name": "Ebco Soft Close Hinge",
        "category": "hardware",
        "quantity": 10.0,
        "unit": "pcs",
        "status": "needed",
    })
    open_hit = step_executors.match_order_open_material(
        sb, project_id="prj-1", incoming_name="Ebco soft close kabje", incoming_unit="pc"
    )
    assert open_hit is not None and open_hit["id"] == "im-hinge"


@pytest.mark.asyncio
async def test_gate_in_mixed_load_rate_pending_enter_rate_and_undo():
    sb = _seed_inv_sb()
    sb.store["item_materials"].append({
        "id": "im-louver",
        "project_id": "prj-1",
        "item_id": None,  # Order-Wide material line
        "material_name": "Charcoal Louver",
        "category": "ply_wood",
        "quantity": 10.0,
        "unit": "pcs",
        "status": "ordered",
    })

    # 1) Mixed load: Inline new Stock SKU + direct Customer Order partial fulfillment (Paid Now)
    posted_expenses: list[dict] = []
    coa_created: list[dict] = []
    deleted_zoho: list[tuple[str, str]] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(
                200,
                json={
                    "chartofaccounts": [
                        {"account_id": "coa-stock-sys", "account_name": "Inventory Asset", "account_type": "stock", "is_system_account": True, "is_active": True},
                        {"account_id": "coa-tax-sys", "account_name": "Advance Tax", "account_type": "other_current_asset", "is_system_account": True, "is_active": True},
                        {"account_id": "coa-op-adj", "account_name": "Opening Balance Adjustments", "account_type": "other_current_liability", "is_system_account": True, "is_active": True},
                        {"account_id": "coa-cap", "account_name": "Capital Stock", "account_type": "equity", "is_system_account": False, "is_active": True},
                        {"account_id": "coa-cogs", "account_name": "Materials", "account_type": "cost_of_goods_sold", "is_system_account": False, "is_active": True},
                        {"account_id": "coa-eq", "account_name": "Opening Balance Offset", "account_type": "equity", "is_system_account": True, "is_active": True},
                    ]
                },
            )
        if method == "POST" and path == "/chartofaccounts":
            coa_created.append(json_body)
            return httpx.Response(200, json={"chart_of_account": {"account_id": "coa-inv-asset", "account_name": json_body["account_name"], "account_type": "other_current_asset"}})
        if method == "GET" and path == "/expenses":
            return httpx.Response(200, json={"expenses": []})
        if method == "POST" and path == "/expenses":
            posted_expenses.append(json_body)
            return httpx.Response(200, json={"expense": {"expense_id": f"ze-{len(posted_expenses)}"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    fake_settings = Settings(zoho_client_id="cid", zoho_client_secret="sec", zoho_refresh_token="rtok", zoho_org_id="org1")
    zoho._COA_CACHE.clear()
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res_load = await step_executors.execute_inventory_load(
            sb,
            settlement_mode="paid_now",
            payment_account_id="acc-axis",
            vendor_id="v-patel",
            lines=[
                {"destination": "stock", "material_name": "18mm Century Ply", "category": "ply_wood", "quantity": 10, "unit": "sheets", "unit_rate_inr": 2000},
                {"destination": "order", "project_id": "prj-1", "material_name": "Charcoal Louver", "quantity": 4, "unit": "pcs", "unit_rate_inr": 500},
            ],
            actor_person_id="p-shr",
        )

    assert res_load["ok"] is True
    assert len(coa_created) == 1 and coa_created[0]["account_name"] == "Workshop Inventory"
    assert len(posted_expenses) == 1
    assert "account_id" not in posted_expenses[0]
    assert len(posted_expenses[0]["line_items"]) == 2
    assert posted_expenses[0]["line_items"][0]["account_id"] == "coa-inv-asset"
    assert posted_expenses[0]["line_items"][1]["account_id"] == "coa-cogs"
    assert posted_expenses[0]["line_items"][1]["tags"] == [{"tag_id": "tag-prj", "tag_option_id": "opt-amb"}]
    # Stock stayed out of customer project cost_cache; only ₹2,000 direct order line added
    assert sb.store["projects"][0]["cost_cache_inr"] == 2000.0
    # Inline SKU created with 10 sheets @ ₹2,000
    assert len(sb.store["inventory_items"]) == 1
    ply_sku = sb.store["inventory_items"][0]
    assert ply_sku["on_hand_qty"] == 10.0 and ply_sku["avg_unit_cost_inr"] == 2000.0
    # Partial order delivery split 10 -> 4 in_shop + 6 ordered
    by_st = {m["status"]: m for m in sb.store["item_materials"]}
    assert by_st["in_shop"]["quantity"] == 4.0
    assert by_st["ordered"]["quantity"] == 6.0

    # 2) Rate Pending Gate-In adds physical qty without Zoho push; [Enter Rate] syncs Zoho WITHOUT double-adding qty
    rp_res = await step_executors.execute_inventory_load(
        sb,
        settlement_mode="rate_pending",
        lines=[{"destination": "stock", "inventory_item_id": ply_sku["id"], "material_name": ply_sku["name"], "quantity": 5, "unit": "sheets"}],
        actor_person_id="p-shr",
    )
    assert rp_res["ok"] is True
    assert rp_res["zoho_sync_status"] == "rate_pending"
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 15.0  # 10 + 5

    rp_mov = rp_res["movements"][0]
    with patch("app.services.zoho.push_mixed_inventory_load_to_zoho", new=AsyncMock(return_value={"ok": True, "stubbed": False, "zoho_type": "bill_open", "zoho_id": "zb-rp-1"})):
        rate_done = await step_executors.complete_rate_pending_movement(
            sb,
            movement_id=rp_mov["id"],
            settlement_mode="vendor_udhaar",
            unit_rate_inr=2300.0,
            vendor_id="v-patel",
            invoice_no="PAT-202",
        )
    assert rate_done["ok"] is True
    # Qty MUST remain 15.0 (no double-add!), weighted avg updated (10*2000 + 5*2300)/15 = 2100.0
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 15.0
    assert sb.store["inventory_items"][0]["avg_unit_cost_inr"] == 2100.0
    assert sb.store["inventory_movements"][-1]["zoho_sync_status"] == "synced"

    # 3) Same-day IST Undo reverses Zoho entry, restores stock & un-splits order row
    first_mov_id = res_load["movements"][0]["id"]
    with patch("app.services.zoho.delete_zoho_financial_entry", side_effect=lambda **kw: deleted_zoho.append((kw["zoho_id"], kw["zoho_type"])) or {"ok": True}):
        undo_res = await step_executors.undo_inventory_movement(sb, first_mov_id, actor_person_id="p-shr")
    assert undo_res["ok"] is True
    assert deleted_zoho == [("ze-1", "expense")]
    assert sb.store["projects"][0]["cost_cache_inr"] == 0.0
    active_mats = [m for m in sb.store["item_materials"]]
    assert len(active_mats) == 1 and active_mats[0]["quantity"] == 10.0 and active_mats[0]["status"] == "ordered"


@pytest.mark.asyncio
async def test_attribution_count_adjust_fifo_ap_and_line_level_sync():
    sb = _seed_inv_sb()
    sku = step_executors.upsert_inventory_item(
        sb, name="Fevicol SH 5kg", category="polish", unit="pcs", avg_unit_cost_inr=1000.0
    )["item"]
    sb.table("inventory_items").update({"on_hand_qty": 6.0}).eq("id", sku["id"]).execute()
    sb.store["item_materials"].append({
        "id": "im-fev",
        "project_id": "prj-1",
        "item_id": None,
        "material_name": "Fevicol SH 5kg",
        "category": "polish",
        "quantity": 2.0,
        "unit": "pcs",
        "status": "needed",
    })

    # 1) Attribution (Order-Wide + Zoho COGS Journal) + auto-advance Material Track
    with patch("app.services.zoho.push_inventory_journal_to_zoho", new=AsyncMock(return_value={"ok": True, "stubbed": False, "zoho_type": "journal", "zoho_id": "zj-attr-1"})):
        attr_res = await step_executors.execute_customer_stock_attribution(
            sb, inventory_item_id=sku["id"], project_id="prj-1", quantity=2.0, item_id=None, actor_person_id="p-shr"
        )
    assert attr_res["ok"] is True
    assert attr_res["issue_cost_inr"] == 2000.0
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 4.0
    assert sb.store["projects"][0]["cost_cache_inr"] == 2000.0
    assert sb.store["item_materials"][0]["status"] == "in_shop"
    assert sb.store["projects"][0]["material_stage"] == "all_in_shop"

    # 2) Count Adjust shrinkage write-off
    with patch("app.services.zoho.push_inventory_journal_to_zoho", new=AsyncMock(return_value={"ok": True, "stubbed": False, "zoho_type": "journal", "zoho_id": "zj-cnt-1"})):
        cnt_res = await step_executors.execute_inventory_count_adjust(
            sb, inventory_item_id=sku["id"], actual_qty=3.0, notes="Lid cracked", actor_person_id="p-shr"
        )
    assert cnt_res["ok"] is True and cnt_res["delta"] == -1.0
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 3.0

    # 3) FIFO Open AP Allocation on vendor_payment + Open Bill creation
    posted_vp: list[dict] = []

    async def fake_fifo_zoho(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/vendorpayments":
            return httpx.Response(200, json={"vendorpayments": []})
        if method == "GET" and path == "/bills":
            return httpx.Response(
                200,
                json={
                    "bills": [
                        {"bill_id": "zb-newer", "date": "2026-11-20", "balance": 9000.0},
                        {"bill_id": "zb-older", "date": "2026-11-10", "balance": 5000.0},
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        if method == "POST" and path == "/vendorpayments":
            posted_vp.append(json_body)
            return httpx.Response(200, json={"vendorpayment": {"payment_id": "zvp-1"}})
        if method == "GET" and path == "/invoices":
            return httpx.Response(200, json={"invoices": [], "page_context": {"has_more_page": False}})
        if method == "GET" and path == "/expenses":
            return httpx.Response(
                200,
                json={
                    "expenses": [
                        {
                            "expense_id": "ze-mix",
                            "status": "nonbillable",
                            "total": 9999.0,
                            "line_items": [
                                {"amount": 2000.0, "tags": [{"tag_option_id": "opt-amb"}]},
                                {"amount": 7999.0, "tags": [{"tag_option_id": "opt-other"}]},
                            ],
                        }
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        if method == "GET" and path == "/journals":
            return httpx.Response(
                200,
                json={
                    "journals": [
                        {
                            "journal_id": "zj-1",
                            "status": "published",
                            "total": 5000.0,
                            "line_items": [
                                {"debit_or_credit": "debit", "amount": 1500.0, "tags": [{"tag_option_id": "opt-amb"}]},
                                {"debit_or_credit": "credit", "amount": 1500.0},
                            ],
                        }
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        raise AssertionError(f"Unexpected call: {method} {path}")

    fake_settings = Settings(zoho_client_id="cid", zoho_client_secret="sec", zoho_refresh_token="rtok", zoho_org_id="org1")
    zoho._COA_CACHE.clear()
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_fifo_zoho),
    ):
        vp_res = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="vendor_payment",
            amount_inr=7000.0,
            payment_account_id="acc-axis",
            party_name="Patel Plywood",
            zoho_contact_id="zv-patel",
            reference_number="MUNIM-FIFO-1",
        )
        assert vp_res["ok"] is True
        assert posted_vp[0]["bills"] == [
            {"bill_id": "zb-older", "amount_applied": 5000.0},
            {"bill_id": "zb-newer", "amount_applied": 2000.0},
        ]

        # 4) Line-level Reporting Tag aggregation skips non-matching lines on shared receipts!
        sync_res = await zoho.sync_project_financial_caches(sb)
        assert sync_res["synced"] == 1

    # ₹2,000 matched expense line + ₹1,500 matched journal debit (7,999 other tag ignored!)
    assert sb.store["projects"][0]["cost_cache_inr"] == 3500.0


@pytest.mark.asyncio
async def test_agent_tools_http_endpoints_and_009_sql_invariants():
    sb = _seed_inv_sb()
    sku = step_executors.upsert_inventory_item(
        sb, name="18mm BWP Ply", category="ply_wood", unit="sheets", min_stock_qty=5, avg_unit_cost_inr=1800.0
    )["item"]
    sb.table("inventory_items").update({"on_hand_qty": 8.0}).eq("id", sku["id"]).execute()

    # 1) search_inventory AI tool
    search_json = await agent_tools.execute_read_tool(sb, "search_inventory", {"query": "BWP", "include_to_buy": True})
    search_data = json.loads(search_json)
    assert len(search_data["stock_items"]) == 1 and search_data["stock_items"][0]["on_hand_qty"] == 8.0

    # 2) AgentCore stage_inventory_action: Rate Pending auto-commits with Undo; attribution pauses for Confirm
    cart = draft_cart.TurnDraftCart()
    stage_rp = json.loads(
        await action_tools.execute_action_tool(
            sb,
            "stage_inventory_action",
            {
                "action_kind": "load",
                "settlement_mode": "rate_pending",
                "lines": [{"destination": "stock", "material_name": "18mm BWP Ply", "quantity": 4, "unit": "sheets"}],
            },
            cart=cart,
            acc_code_to_id={"axis": "acc-axis"},
            ui_context=None,
            user_text="4 sheet 18mm BWP ply aayi hai rate pending",
            heard_transcript=None,
        )
    )
    assert stage_rp["ok"] is True and stage_rp["staged"] is True
    turn_rp = await agent_core._process_goal_intent_item(
        sb,
        item=cart.items()[0],
        acc_code_to_id={"axis": "acc-axis"},
        owner_person_id="p-shr",
        actor_label="Shreyansh",
        source_channel="web_command_bar",
        domain_scope="inventory",
        thinking_level="low",
        heard_transcript=None,
        uploaded_gcs_paths=[],
        ui_context={},
        telegram_chat_id=None,
    )
    assert turn_rp["status"] == "completed"
    assert turn_rp["undo_available"] is True
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 12.0

    # Undo committed AgentCore goal reverts stock cleanly back to 8.0
    undo_rp = await agent_core.undo_completed_goal_async(sb, goal_id=turn_rp["goal_id"], actor_person_id="p-shr")
    assert undo_rp["ok"] is True
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 8.0

    cart2 = draft_cart.TurnDraftCart()
    stage_attr = json.loads(
        await action_tools.execute_action_tool(
            sb,
            "stage_inventory_action",
            {
                "action_kind": "attribute",
                "inventory_item_id": sku["id"],
                "project_id": "prj-1",
                "quantity": 2.0,
            },
            cart=cart2,
            acc_code_to_id={"axis": "acc-axis"},
            ui_context=None,
            user_text="2 sheet 18mm BWP ply Ambure Ji ko issue karo",
            heard_transcript=None,
        )
    )
    assert stage_attr["ok"] is True
    turn_attr = await agent_core._process_goal_intent_item(
        sb,
        item=cart2.items()[0],
        acc_code_to_id={"axis": "acc-axis"},
        owner_person_id="p-shr",
        actor_label="Shreyansh",
        source_channel="web_command_bar",
        domain_scope="inventory",
        thinking_level="low",
        heard_transcript=None,
        uploaded_gcs_paths=[],
        ui_context={},
        telegram_chat_id=None,
    )
    assert turn_attr["status"] == "awaiting_confirmation"
    with patch("app.services.zoho.push_inventory_journal_to_zoho", new=AsyncMock(return_value={"ok": True, "stubbed": True, "zoho_type": "journal", "zoho_id": "STUB-1"})):
        conf_res = await agent_core.confirm_and_execute_goal(
            sb, goal_id=turn_attr["goal_id"], actor_person_id="p-shr", actor_label="Shreyansh"
        )
    assert conf_res["status"] == "completed"
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 6.0

    # 3) HTTP Endpoints allow can_login_munim=False operator uniformly + support Order-Wide materials
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="google", identifier="clerk@example.com", display_name="Ramesh Clerk"
    )
    try:
        with (
            patch("app.routers.domains.get_supabase_admin", return_value=sb),
            patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r_ov = await client.get("/api/inventory/overview")
                assert r_ov.status_code == 200 and len(r_ov.json()["items"]) == 1

                r_ow = await client.post(
                    "/api/production/orders/prj-1/materials",
                    json={"material_name": "Edge Band Tape", "quantity": 2, "unit": "roll"},
                )
                assert r_ow.status_code == 200

                r_load = await client.post(
                    "/api/inventory/load",
                    json={
                        "settlement_mode": "manual",
                        "lines": [{"destination": "stock", "material_name": "Hinges 0-Crank", "quantity": 20, "unit": "pcs", "unit_rate_inr": 85}],
                    },
                )
                assert r_load.status_code == 200
                mov_id = r_load.json()["movements"][0]["id"]

                r_rp_bad = await client.post(
                    f"/api/inventory/movements/{mov_id}/enter-rate",
                    json={"settlement_mode": "paid_now", "unit_rate_inr": 90, "payment_account_id": "acc-axis"},
                )
                assert r_rp_bad.status_code == 400
    finally:
        app.dependency_overrides.clear()

    # 4) 009_inventory_module.sql security & schema invariants (ignoring comment lines)
    sql = (REPO_ROOT / "supabase" / "migrations" / "009_inventory_module.sql").read_text(encoding="utf-8")
    sql_nc = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "ENABLE ROW LEVEL SECURITY" in sql_nc
    assert "WITH (security_invoker = true)" in sql_nc
    assert "TO authenticated" in sql_nc
    assert "USING (" in sql_nc and "WITH CHECK (" in sql_nc
    assert "SECURITY DEFINER" not in sql_nc.upper()
    assert "CREATE TYPE" not in sql_nc.upper()
    assert "INSERT INTO public.inventory_items" not in sql_nc


@pytest.mark.asyncio
async def test_zoho_notes_sanitization_retry_idempotency_and_hardening():
    from app.services.zoho.inventory_ledger import _clean_zoho_text

    # 1) Long SKU + '->' / '<>' sanitized <= 95 chars and free of forbidden chars
    raw_note = "[PRJ-OCT-012] ORIGIN: WRD. LIFT 10KG W650-920MM CDGREY (805.11.356) (1 pcs) -> <Chair Chittorgarh>"
    clean = _clean_zoho_text(f"[Inventory] {raw_note}", 95)
    assert len(clean) <= 95
    assert "<" not in clean and ">" not in clean and "->" not in clean and " for " in clean

    # 2) Journal payload uses sanitized notes (no top-level order_tags)
    posted_journals: list[dict] = []

    async def fake_jrn_zoho(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/journals":
            return httpx.Response(200, json={"journals": []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(
                200,
                json={
                    "chartofaccounts": [
                        {"account_id": "coa-wi", "account_name": "Workshop Inventory", "account_type": "other_current_asset", "is_system_account": False, "is_active": True},
                        {"account_id": "coa-mat", "account_name": "Materials", "account_type": "cost_of_goods_sold", "is_system_account": False, "is_active": True},
                    ]
                },
            )
        if method == "POST" and path == "/journals":
            posted_journals.append(json_body)
            return httpx.Response(201, json={"journal": {"journal_id": "zj-san"}})
        raise AssertionError(f"Unexpected call: {method} {path}")

    sb = _seed_inv_sb()
    sku = step_executors.upsert_inventory_item(
        sb, name="ORIGIN: WRD. LIFT 10KG W650-920MM CDGREY (805.11.356)", category="hardware", unit="pcs", avg_unit_cost_inr=5440.0
    )["item"]
    sb.table("inventory_items").update({"on_hand_qty": 2.0}).eq("id", sku["id"]).execute()

    fake_settings = Settings(zoho_client_id="cid", zoho_client_secret="sec", zoho_refresh_token="rtok", zoho_org_id="org1")
    zoho._COA_CACHE.clear()
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_jrn_zoho),
    ):
        attr = await step_executors.execute_customer_stock_attribution(
            sb,
            inventory_item_id=sku["id"],
            project_id="prj-1",
            quantity=1.0,
            item_id="",
            item_material_id="new_unlisted",
            actor_person_id="",
            reference_number="MUNIM-RETRY-1",
        )
        assert attr["ok"] is True
        assert sb.store["inventory_movements"][0]["item_id"] is None
        assert sb.store["inventory_movements"][0]["actor_person_id"] is None
        assert len(posted_journals[0]["notes"]) <= 95
        assert "<" not in posted_journals[0]["notes"] and ">" not in posted_journals[0]["notes"]
        assert "tags" not in posted_journals[0]  # only on debit line item

        # 3) Cloud Tasks retry with same reference_number dedups cleanly without double-deducting qty!
        retry = await step_executors.execute_customer_stock_attribution(
            sb,
            inventory_item_id=sku["id"],
            project_id="prj-1",
            quantity=1.0,
            reference_number="MUNIM-RETRY-1",
        )
        assert retry["ok"] is True and retry["deduped"] is True
        assert sb.store["inventory_items"][0]["on_hand_qty"] == 1.0  # NOT 0.0!

    # 4) Duplicate bill detection on vendor_udhaar + Enter Rate <= 0 rejection
    sb2 = _seed_inv_sb()
    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._find_existing_by_reference", AsyncMock(return_value=None)),
        patch("app.services.zoho.check_duplicate_bill", AsyncMock(return_value={"source": "zoho_books", "match": {"bill_id": "zb-old"}})),
    ):
        dup_load = await step_executors.execute_inventory_load(
            sb2,
            settlement_mode="vendor_udhaar",
            vendor_id="v-patel",
            invoice_no="INV-DUP-1",
            lines=[{"destination": "stock", "material_name": "18mm Ply", "quantity": 2, "unit": "sheets", "unit_rate_inr": 1500}],
        )
        assert dup_load["ok"] is False and dup_load["error"]["code"] == "DUPLICATE_BILL"

    rp = await step_executors.execute_inventory_load(
        sb2,
        settlement_mode="rate_pending",
        lines=[{"destination": "stock", "material_name": "18mm Ply", "quantity": 2, "unit": "sheets"}],
    )
    zero_rate = await step_executors.complete_rate_pending_movement(
        sb2, movement_id=rp["movements"][0]["id"], settlement_mode="manual", unit_rate_inr=0.0
    )
    assert zero_rate["ok"] is False


@pytest.mark.asyncio
async def test_zoho_contact_creation_omits_gst_fields_and_surfaces_vendor_error():
    """GST-off Zoho org rejects gst_no/gst_treatment/phone; fold into notes and surface real errors."""
    sb = _seed_inv_sb()
    fake_settings = Settings(
        zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o"
    )
    posted_contacts: list[dict[str, object]] = []

    async def fake_zoho_req(client, method, path, params=None, json_body=None):
        if method == "POST" and path == "/contacts":
            posted_contacts.append(dict(json_body or {}))
            return httpx.Response(201, json={"contact": {"contact_id": "zv-nav", "contact_name": "Navkar Traders"}})
        raise AssertionError(f"Unexpected Zoho request: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res = await zoho.create_zoho_contact(
            "Navkar Traders", "vendor", phone="9414009486", gstin="08JCLPK6516H1Z6"
        )
    assert res["ok"] is True
    assert set(posted_contacts[0].keys()) == {"contact_name", "contact_type", "notes"}
    assert "Phone: 9414009486" in str(posted_contacts[0]["notes"])
    assert "GSTIN: 08JCLPK6516H1Z6" in str(posted_contacts[0]["notes"])

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._find_existing_by_reference", AsyncMock(return_value=None)),
        patch("app.services.zoho._get_chart_of_accounts", AsyncMock(return_value=[{"account_id": "a1", "account_name": "Inventory Asset", "account_type": "stock", "is_active": True}, {"account_id": "a2", "account_name": "Cost of Goods Sold", "account_type": "cost_of_goods_sold", "is_active": True}])),
        patch("app.services.zoho.resolve_payee_zoho_vendor", AsyncMock(return_value={"contact_id": None, "error": "Zoho Contact error 400: Invalid GST"})),
    ):
        fail_res = await zoho.push_mixed_inventory_load_to_zoho(
            sb,
            settlement_mode="vendor_udhaar",
            lines=[{"destination": "stock", "material_label": "Fibre", "quantity": 10, "unit": "mtr", "unit_rate_inr": 110, "total_inr": 1100}],
            reference_number="MUNIM-TEST-ERR",
            vendor_name="Navkar Traders",
        )
    assert fail_res["ok"] is False and "Zoho Contact error 400: Invalid GST" in fail_res["error"]["message"]


@pytest.mark.asyncio
async def test_inventory_load_vendor_matching_followup_routing_and_auto_vendor_upsert():
    """Inventory load enriches vendor badges, routes 'for <Customer>', and auto-upserts new vendors."""
    sb = _seed_inv_sb()
    sb.store["vendors"] = [
        {"id": "v-nav", "canonical_name": "Navkar Traders", "vendor_kind": "supplier", "zoho_contact_id": "zv-nav", "aliases": [], "is_active": True}
    ]
    from app.services.inventory_ai import format_inventory_confirmation_summary, resolve_inventory_slots

    cand_matched = agent_core.GoalIntentItem(
        operation_kind="inventory_load", summary_title="Load Fibre", party_name="Navkar Traders",
        settlement_mode="vendor_udhaar", invoice_no="408",
        inventory_lines_json='[{"destination":"stock","material_name":"Fibre","quantity":10,"unit":"mtr","unit_rate_inr":110}]',
    )
    await draft_cart.enrich_candidate_entities(sb, cand_matched)
    assert cand_matched.is_new_payee is False and cand_matched.vendor_id == "v-nav"
    card_m = format_inventory_confirmation_summary(sb, "inventory_load", cand_matched.model_dump())
    assert "✅ Matched" in card_m and "Invoice: `408`" in card_m

    cand_new = agent_core.GoalIntentItem(
        operation_kind="inventory_load", summary_title="Load Mattress", party_name="Ganesh Steel",
        settlement_mode="vendor_udhaar", invoice_no="656",
        inventory_lines_json='[{"destination":"stock","material_name":"Mattress","quantity":1,"unit":"pcs","unit_rate_inr":8500}]',
    )
    with patch("app.services.zoho.search_zoho_contacts", AsyncMock(return_value=[])):
        await draft_cart.enrich_candidate_entities(sb, cand_new)
    assert cand_new.is_new_payee is True and cand_new.vendor_id is None
    card_n = format_inventory_confirmation_summary(sb, "inventory_load", cand_new.model_dump())
    assert "🆕 New Vendor" in card_n and "Invoice: `656`" in card_n

    # Follow-up "for Kaushab" converts stock lines on inventory_load to direct order lines
    slots = {"operation_kind": "inventory_load", "project_id": "Kaushab", "inventory_lines_json": cand_new.inventory_lines_json}
    resolve_inventory_slots(sb, slots, lambda _sb, h: "prj-k" if "kaushab" in h.lower() else None)
    assert slots["project_id"] == "prj-k" and json.loads(slots["inventory_lines_json"])[0]["destination"] == "order"

    # Confirming an inventory_load for a new vendor auto-upserts Supabase vendors & links vendor_id
    fake_settings = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    async def fake_req(client, method, path, params=None, json_body=None):
        if method == "GET" and path == "/bills":
            return httpx.Response(200, json={"bills": []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": [
                {"account_id": "a-inv", "account_name": "Workshop Inventory", "account_type": "other_current_asset", "is_active": True},
                {"account_id": "a-cogs", "account_name": "Materials", "account_type": "expense", "is_active": True},
            ]})
        if method == "GET" and path == "/contacts":
            return httpx.Response(200, json={"contacts": []})
        if method == "POST" and path == "/contacts":
            return httpx.Response(201, json={"contact": {"contact_id": "zv-ganesh", "contact_name": "Ganesh Steel Industries"}})
        if method == "POST" and path == "/bills":
            return httpx.Response(201, json={"bill": {"bill_id": "zb-656", "status": "open"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_req),
        patch("app.services.zoho.check_duplicate_bill", AsyncMock(return_value=None)),
    ):
        load_res = await step_executors.execute_inventory_load(
            sb, settlement_mode="vendor_udhaar", vendor_name="Ganesh Steel Industries",
            vendor_phone="9414010651", vendor_gstin="08AADPI5716Q1ZA", invoice_no="656",
            lines=[{"destination": "stock", "material_name": "Mattress - D.ortho plus 78x48", "quantity": 1, "unit": "pcs", "unit_rate_inr": 8500}],
        )
    assert load_res["ok"] is True
    assert any(v["canonical_name"] == "Ganesh Steel Industries" and v["zoho_contact_id"] == "zv-ganesh" and v["gstin"] == "08AADPI5716Q1ZA" for v in sb.store["vendors"])
    assert load_res["movements"][0]["vendor_id"] is not None
    assert step_executors.infer_material_category("Plastic Strip 25mm*0.80mm solid hg [1013]") == "laminate"
    assert step_executors.infer_material_category("Fibre") == "foam_fabric"



