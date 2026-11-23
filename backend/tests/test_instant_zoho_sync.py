"""Tests for coalesced instant Zoho balance sync, draft/bill Order P&L updates, and 5-min stale guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import Settings
from app.services import agent_core, step_executors, zoho
from app.services.agent_core.financial_amendments import (
    run_async_financial_or_stage_undo,
    undo_zoho_steps_and_project_snapshots,
)
from app.services.zoho import sync as zoho_sync
from conftest import FakeSupabase, seed_goal_intelligence_sb


def _live_zoho_settings() -> Settings:
    return Settings(
        _env_file=None,
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org-instant-test",
    )


class _CountingTable:
    def __init__(self, inner: Any, counter: list[int], is_pa: bool):
        self._inner = inner
        self._counter = counter
        self._is_pa = is_pa

    def select(self, *args: Any, **kwargs: Any):
        return self._inner.select(*args, **kwargs)

    def insert(self, *args: Any, **kwargs: Any):
        return self._inner.insert(*args, **kwargs)

    def delete(self):
        return self._inner.delete()

    def update(self, patch_obj: dict[str, Any]):
        if self._is_pa:
            self._counter[0] += 1
        return self._inner.update(patch_obj)


class _CountingSupabase(FakeSupabase):
    def __init__(self, initial: dict[str, list[dict[str, Any]]]):
        super().__init__(initial)
        self.pa_update_calls = [0]

    def table(self, name: str):
        return _CountingTable(super().table(name), self.pa_update_calls, name == "payment_accounts")


@pytest.mark.asyncio
async def test_coalesce_zoho_sync_selective_writes_and_error_isolation():
    sb = _CountingSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-cash",
                    "code": "cash_galla",
                    "display_name": "Cash",
                    "account_kind": "cash_galla",
                    "zoho_account_id": "za-cash",
                    "cached_balance_inr": 130000.0,
                    "balance_synced_at": "2026-11-23T08:00:00+00:00",
                    "is_active": True,
                    "sort_order": 1,
                },
                {
                    "id": "acc-rgb",
                    "code": "rgb",
                    "display_name": "RGB",
                    "account_kind": "bank",
                    "zoho_account_id": "za-rgb",
                    "cached_balance_inr": 42469.68,
                    "balance_synced_at": "2026-11-23T08:00:00+00:00",
                    "is_active": True,
                    "sort_order": 2,
                },
            ],
            "people": [{"id": "p-shr", "full_name": "Shreyansh", "person_type": "operator", "is_active": True}],
            "vendors": [{"id": "v-1", "canonical_name": "Singh Plywood", "is_active": True}],
            "customers": [],
            "projects": [],
            "stage_events": [],
            "agent_goals": [
                {
                    "id": "g-multi",
                    "owner_person_id": "p-shr",
                    "source_channel": "telegram",
                    "domain_scope": "munim",
                    "goal_type": "financial_txn",
                    "status": "awaiting_confirmation",
                    "summary_title": "Expense + Transfer",
                    "collected_slots": {
                        "operation_kind": "expense",
                        "party_name": "Singh Plywood",
                        "amount_inr": 100000.0,
                        "payment_account_id": "acc-cash",
                        "payment_account_code": "cash_galla",
                    },
                    "steps": [
                        {
                            "step_index": 0,
                            "operation": "expense",
                            "status": "pending",
                            "payload": {
                                "party_name": "Singh Plywood",
                                "amount_inr": 100000.0,
                                "payment_account_id": "acc-cash",
                            },
                        },
                        {
                            "step_index": 1,
                            "operation": "float_transfer",
                            "status": "pending",
                            "payload": {
                                "amount_inr": 5000.0,
                                "payment_account_id": "acc-cash",
                                "to_payment_account_id": "acc-rgb",
                            },
                        },
                    ],
                    "undo_event_ids": [],
                    "metadata": {},
                }
            ],
        }
    )

    bankaccounts_calls = [0]

    async def fake_zoho_req(_client, method: str, path: str, **_kwargs):
        if method == "GET" and path == "/bankaccounts":
            bankaccounts_calls[0] += 1
            return httpx.Response(
                200,
                json={
                    "bankaccounts": [
                        {"account_id": "za-cash", "account_name": "Cash Galla", "account_type": "cash", "balance": 25000.0, "is_active": True},
                        {"account_id": "za-rgb", "account_name": "RGB", "account_type": "bank", "balance": 42469.68, "is_active": True},
                    ]
                },
            )
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=_live_zoho_settings()),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
        patch(
            "app.services.zoho.push_management_entry_to_zoho",
            AsyncMock(side_effect=[
                {"ok": True, "status_code": 200, "zoho_id": "ze-1", "zoho_type": "expense"},
                {"ok": True, "status_code": 200, "zoho_id": "zt-1", "zoho_type": "transfer_fund"},
            ]),
        ),
    ):
        out = await agent_core.confirm_and_execute_goal(
            sb, goal_id="g-multi", actor_person_id="p-shr", actor_label="Shreyansh"
        )

    assert out["status"] == "completed"
    # Multi-step confirmation makes exactly 1 /bankaccounts call and writes ONLY the changed row (Cash)!
    assert bankaccounts_calls[0] == 1
    assert sb.pa_update_calls[0] == 1
    assert sb.store["payment_accounts"][0]["cached_balance_inr"] == 25000.0
    assert sb.store["payment_accounts"][1]["cached_balance_inr"] == 42469.68

    # Network hiccup during post-mutation sync must NEVER fail committed goal!
    sb.store["agent_goals"].append(
        {
            "id": "g-net-err",
            "owner_person_id": "p-shr",
            "source_channel": "web_command_bar",
            "domain_scope": "munim",
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Expense: Chai",
            "collected_slots": {
                "operation_kind": "expense",
                "party_name": "Chai",
                "amount_inr": 200.0,
                "payment_account_id": "acc-cash",
            },
            "steps": [{"step_index": 0, "operation": "expense", "status": "pending", "payload": {}}],
            "undo_event_ids": [],
            "metadata": {},
        }
    )
    with (
        patch("app.services.zoho.get_settings", return_value=_live_zoho_settings()),
        patch("app.services.zoho.sync_owner_pulse_snapshot", AsyncMock(side_effect=RuntimeError("Zoho read blip"))),
        patch(
            "app.services.zoho.push_management_entry_to_zoho",
            AsyncMock(return_value={"ok": True, "status_code": 200, "zoho_id": "ze-2", "zoho_type": "expense"}),
        ),
    ):
        err_out = await agent_core.confirm_and_execute_goal(
            sb, goal_id="g-net-err", actor_person_id="p-shr", actor_label="Shreyansh"
        )
    assert err_out["status"] == "completed"


@pytest.mark.asyncio
async def test_undo_and_inventory_flows_trigger_balance_sync():
    now_iso = datetime.now(timezone.utc).isoformat()
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {"id": "acc-cash", "code": "cash_galla", "display_name": "Cash", "account_kind": "cash_galla", "zoho_account_id": "za-cash", "cached_balance_inr": 100000.0, "is_active": True},
            ],
            "vendors": [{"id": "v-1", "canonical_name": "Singh Plywood", "zoho_contact_id": "zv-1", "is_active": True}],
            "people": [{"id": "p-shr", "full_name": "Shreyansh", "person_type": "operator", "is_active": True}],
            "inventory_items": [],
            "inventory_movements": [],
            "projects": [],
            "item_materials": [],
            "stage_events": [],
            "agent_goals": [
                {
                    "id": "g-undo-fin",
                    "status": "completed",
                    "created_at": now_iso,
                    "collected_slots": {"operation_kind": "expense"},
                    "steps": [{"operation": "expense", "result": {"ok": True, "zoho_id": "ze-99", "zoho_type": "expense"}}],
                    "undo_event_ids": [],
                }
            ],
        }
    )

    sync_mock = AsyncMock(return_value={"ok": True, "accounts_synced": 1})
    with (
        patch("app.services.zoho.get_settings", return_value=_live_zoho_settings()),
        patch("app.services.zoho.sync_owner_pulse_snapshot", sync_mock),
        patch("app.services.zoho.delete_zoho_financial_entry", AsyncMock(return_value={"ok": True, "deleted": True})),
        patch(
            "app.services.zoho.push_mixed_inventory_load_to_zoho",
            AsyncMock(return_value={"ok": True, "stubbed": False, "zoho_type": "expense", "zoho_id": "ze-inv-1"}),
        ),
    ):
        undo_res = await run_async_financial_or_stage_undo(
            sb, goal_id="g-undo-fin", actor_person_id="p-shr", sync_undo_fn=lambda *_a, **_k: {"ok": True}
        )
        assert undo_res["ok"] is True

        inv_load = await step_executors.execute_inventory_load(
            sb,
            settlement_mode="paid_now",
            payment_account_id="acc-cash",
            vendor_id="v-1",
            lines=[{"destination": "stock", "material_name": "18mm Ply", "quantity": 5, "unit": "sheets", "unit_rate_inr": 1800}],
            actor_person_id="p-shr",
        )
        assert inv_load["ok"] is True

        mov_id = inv_load["movements"][0]["id"]
        inv_undo = await step_executors.undo_inventory_movement(sb, mov_id, actor_person_id="p-shr")
        assert inv_undo["ok"] is True

    assert sync_mock.call_count == 3


@pytest.mark.asyncio
async def test_supplier_bill_and_draft_invoice_update_order_pnl_and_undo():
    sb = seed_goal_intelligence_sb()
    sb.store["projects"][0]["cost_cache_inr"] = 10000.0
    sb.store["projects"][0]["zoho_bills_cache"] = 10000.0
    sb.store["projects"][0]["zoho_invoiced_cache"] = 50000.0

    with (
        patch("app.services.zoho.check_duplicate_bill", AsyncMock(return_value=None)),
        patch("app.services.zoho.resolve_payee_zoho_vendor", AsyncMock(return_value={"contact_id": "zv-rp", "canonical_name": "R P Wood"})),
        patch("app.services.zoho.create_draft_bill", AsyncMock(return_value={"bill": {"bill_id": "zb-1", "status": "open"}, "expense_account_name": "Materials"})),
        patch("app.services.zoho.create_draft_invoice", AsyncMock(return_value={"invoice": {"invoice_id": "zi-1", "status": "draft"}})),
    ):
        bill_res = await step_executors.execute_munim_draft_doc_step(
            sb,
            goal_id="g-bill",
            step_index=0,
            op="supplier_draft_bill",
            slots={"party_name": "R P Wood", "amount_inr": 25000.0, "project_id": "prj-ambure", "invoice_no": "RP-1"},
        )
        inv_res = await step_executors.execute_munim_draft_doc_step(
            sb,
            goal_id="g-inv",
            step_index=0,
            op="customer_draft_invoice",
            slots={"party_name": "Ambure Ji", "amount_inr": 100000.0, "project_id": "prj-ambure"},
        )

    assert bill_res["ok"] is True
    assert inv_res["ok"] is True
    prj = sb.store["projects"][0]
    assert prj["zoho_bills_cache"] == 35000.0
    assert prj["cost_cache_inr"] == 35000.0
    assert prj["zoho_invoiced_cache"] == 150000.0

    with patch("app.services.zoho.delete_zoho_financial_entry", AsyncMock(return_value={"ok": True})):
        _, errs = await undo_zoho_steps_and_project_snapshots(
            sb,
            {
                "steps": [
                    {"operation": "supplier_draft_bill", "result": bill_res},
                    {"operation": "customer_draft_invoice", "result": inv_res},
                ]
            },
        )
    assert errs == []
    assert prj["zoho_bills_cache"] == 10000.0
    assert prj["cost_cache_inr"] == 10000.0
    assert prj["zoho_invoiced_cache"] == 50000.0


@pytest.mark.asyncio
async def test_ensure_fresh_owner_pulse_respects_5min_ttl():
    zoho_sync._LAST_PULSE_SYNC_MONO.clear()
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    fresh_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

    sb_stale = FakeSupabase({"payment_accounts": [{"id": "a1", "code": "cash_galla", "balance_synced_at": stale_ts, "is_active": True}]})
    sb_fresh = FakeSupabase({"payment_accounts": [{"id": "a1", "code": "cash_galla", "balance_synced_at": fresh_ts, "is_active": True}]})

    sync_mock = AsyncMock(return_value={"ok": True, "accounts_synced": 1})
    with (
        patch("app.services.zoho.get_settings", return_value=_live_zoho_settings()),
        patch("app.services.zoho.sync_owner_pulse_snapshot", sync_mock),
    ):
        await zoho.ensure_fresh_owner_pulse(sb_fresh, max_age_seconds=300.0)
        assert sync_mock.call_count == 0

        await zoho.ensure_fresh_owner_pulse(sb_stale, max_age_seconds=300.0)
        assert sync_mock.call_count == 1
