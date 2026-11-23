from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.agent_core import slot_resolvers
from app.services.zoho.ledger import _pick_matching_zoho_bank_account
from tests.conftest import FakeQuery, FakeSupabase, FakeTable

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_010 = REPO_ROOT / "supabase" / "migrations" / "010_six_active_accounts.sql"
RESET_SCRIPT_PATH = REPO_ROOT / "scripts" / "reset_clean_slate.py"


def _load_reset_module():
    spec = importlib.util.spec_from_file_location("reset_clean_slate", RESET_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ResetFakeQuery(FakeQuery):
    """Local FakeQuery extension supporting `.neq()` and `.gte()` PostgREST filters."""

    def neq(self, col: str, val: Any) -> "_ResetFakeQuery":
        self._filters.append(lambda r: r.get(col) != val)
        return self

    def gte(self, col: str, val: Any) -> "_ResetFakeQuery":
        self._filters.append(lambda r: r.get(col) is not None and r.get(col) >= val)
        return self


class _ResetFakeTable(FakeTable):
    def _q(self) -> _ResetFakeQuery:
        return _ResetFakeQuery(self.rows)


class _ResetFakeSupabase(FakeSupabase):
    def table(self, name: str) -> _ResetFakeTable:
        return _ResetFakeTable(self.store.setdefault(name, []))


def test_migration_010_enforces_security_invariants_and_six_accounts():
    sql = MIGRATION_010.read_text(encoding="utf-8")
    non_comment_sql = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).lower()

    assert "security definer" not in non_comment_sql
    assert "auth.role()" not in non_comment_sql
    assert "user_metadata" not in non_comment_sql
    assert "create type " not in non_comment_sql

    for code in ("cash_galla", "axis", "rgb", "ccrgb", "float_rohit", "float_kamlesh"):
        assert f"'{code}'" in sql
    assert "NOT IN" in sql


def test_pick_matching_zoho_bank_account_matches_literal_cash_safely():
    zoho_accs = [
        {"account_id": "z-cash", "account_name": "Cash", "account_type": "cash", "is_active": True},
        {"account_id": "z-rohit", "account_name": "Rohit Wallet", "account_type": "cash", "is_active": True},
        {"account_id": "z-ccrgb", "account_name": "CCRGB", "account_type": "bank", "is_active": True},
        {"account_id": "z-rgb", "account_name": "RGB", "account_type": "bank", "is_active": True},
    ]

    cash_local = {
        "code": "cash_galla",
        "display_name": "Cash",
        "account_kind": "cash_galla",
        "aliases": ["cash", "cash galla"],
    }
    rohit_local = {
        "code": "float_rohit",
        "display_name": "Rohit Wallet",
        "account_kind": "supervisor_float",
        "aliases": ["rohit", "rohit cash"],
    }
    rgb_local = {
        "code": "rgb",
        "display_name": "RGB",
        "account_kind": "bank",
        "aliases": ["rgb"],
    }

    # Even if float_rohit is checked first, it must never steal Zoho's literal "Cash" account.
    rohit_hit = _pick_matching_zoho_bank_account(rohit_local, zoho_accs, set())
    assert rohit_hit is not None and rohit_hit["account_id"] == "z-rohit"

    cash_hit = _pick_matching_zoho_bank_account(cash_local, zoho_accs, {"z-rohit"})
    assert cash_hit is not None and cash_hit["account_id"] == "z-cash"

    # Exact match ensures RGB does not collide with CCRGB.
    rgb_hit = _pick_matching_zoho_bank_account(rgb_local, zoho_accs, {"z-rohit", "z-cash"})
    assert rgb_hit is not None and rgb_hit["account_id"] == "z-rgb"


def test_quick_pick_account_choices_prioritize_active_six():
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {"code": "ccrgb", "display_name": "CCRGB", "account_kind": "od_cc", "is_active": True, "sort_order": 4},
                {"code": "float_rohit", "display_name": "Rohit Wallet", "account_kind": "supervisor_float", "is_active": True, "sort_order": 5},
                {"code": "axis", "display_name": "Axis Bank", "account_kind": "bank", "is_active": True, "sort_order": 2},
                {"code": "cash_galla", "display_name": "Cash", "account_kind": "cash_galla", "is_active": True, "sort_order": 1},
                {"code": "rgb", "display_name": "RGB", "account_kind": "bank", "is_active": True, "sort_order": 3},
                {"code": "float_kamlesh", "display_name": "Kamlesh Wallet", "account_kind": "supervisor_float", "is_active": True, "sort_order": 6},
            ]
        }
    )
    choices = slot_resolvers._build_default_missing_slot_choices(sb, "payment_account_code", "expense")
    codes = [c["choice_id"] for c in choices]
    assert codes == ["acc_cash_galla", "acc_axis", "acc_rgb", "acc_float_rohit"]


def _seed_dirty_sb() -> _ResetFakeSupabase:
    return _ResetFakeSupabase(
        {
            "inventory_movements": [{"id": "im-1", "material_label": "18mm Ply"}],
            "worker_payroll_entries": [{"id": "wp-1", "amount_inr": 5000}],
            "monthly_fnf_sheets": [{"id": "fnf-1", "salary_month": "2026-10"}],
            "worker_attendance": [{"id": "wa-1", "work_date": "2026-11-10"}],
            "cash_ledger": [{"id": "cl-1", "amount_inr": 10000}],
            "agent_goals": [{"id": "ag-1", "status": "parked_for_clerk"}],
            "telegram_state": [{"id": "ts-1", "telegram_update_id": 101}],
            "telegram_inbox_buffer": [{"id": "tib-1", "telegram_update_id": 102}],
            "telegram_webhook_dedup": [{"telegram_update_id": 102, "chat_id": 1}],
            "followups": [{"id": "fu-1", "name": "Ambure Ji"}],
            "inventory_items": [
                {
                    "id": "sku-1",
                    "name": "18mm Gurjan Ply",
                    "on_hand_qty": 25.0,
                    "avg_unit_cost_inr": 1850.0,
                    "last_purchase_rate_inr": 1900.0,
                }
            ],
            "customers": [{"id": "cust-1", "full_name": "Ambure Ji", "zoho_contact_id": "zc-1", "is_active": True}],
            "vendors": [{"id": "vend-1", "canonical_name": "Manish Cushioning", "vendor_kind": "contractor", "is_active": True}],
            "people": [
                {"id": "p-shreyansh", "full_name": "Shreyansh", "person_type": "operator", "is_active": True},
                {"id": "p-kamlesh", "full_name": "Kamlesh Bhaiya", "person_type": "operator", "is_active": True},
                {"id": "p-arvind", "full_name": "Arvind Ji", "person_type": "salaried_karigar", "is_active": True},
                {"id": "p-manish", "full_name": "Manish Cushioning", "person_type": "contractor_mistri", "is_active": True},
            ],
            "projects": [
                {
                    "id": "prj-1",
                    "project_code": "PRJ-OCT-001",
                    "project_type": "customer",
                    "customer_id": "cust-1",
                    "quoted_total": 2300000.0,
                    "paid_cache_inr": 1500000.0,
                    "cost_cache_inr": 450000.0,
                    "zoho_invoiced_cache": 500000.0,
                    "zoho_bills_cache": 200000.0,
                    "payment_stage": "partial_paid",
                    "zoho_synced_at": "2026-11-10T00:00:00Z",
                    "estimator_ids": ["p-manish", "p-arvind"],
                    "current_owner_ids": ["p-shreyansh", "p-manish"],
                },
                {
                    "id": "prj-gen",
                    "project_code": "GEN-STOCK",
                    "project_type": "general_stock",
                    "paid_cache_inr": 100.0,
                    "cost_cache_inr": 5000.0,
                    "zoho_invoiced_cache": 0.0,
                    "zoho_bills_cache": 5000.0,
                    "estimator_ids": [],
                    "current_owner_ids": [],
                },
            ],
            "project_items": [
                {"id": "item-1", "project_id": "prj-1", "item_name": "Kitchen", "stage": "at_site"}
            ],
            "payment_accounts": [
                {"id": "a-cash", "code": "cash_galla", "display_name": "Cash Galla", "account_kind": "cash_galla", "is_active": True, "cached_balance_inr": 50000.0, "zoho_account_id": "z-cash"},
                {"id": "a-axis", "code": "axis", "display_name": "Axis Bank", "account_kind": "bank", "is_active": True, "cached_balance_inr": 120000.0},
                {"id": "a-icici", "code": "icici", "display_name": "ICICI Bank", "account_kind": "bank", "is_active": True, "cached_balance_inr": 9000.0, "zoho_account_id": "z-old"},
                {"id": "a-ansh", "code": "float_ansh", "display_name": "Ansh Wallet", "account_kind": "supervisor_float", "is_active": True, "cached_balance_inr": 4000.0},
            ],
        }
    )


def test_reset_supabase_dry_run_and_execute():
    mod = _load_reset_module()
    sb = _seed_dirty_sb()

    dry = mod.reset_supabase_data(sb, execute=False)
    assert dry["executed"] is False
    assert dry["wiped_tables"]["cash_ledger"] == 1
    assert dry["contractor_mistris_removed"] == ["Manish Cushioning"]
    # Zero mutations in dry-run!
    assert len(sb.store["cash_ledger"]) == 1
    assert sb.store["projects"][0]["paid_cache_inr"] == 1500000.0
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 25.0
    assert len(sb.store["people"]) == 4

    live = mod.reset_supabase_data(sb, execute=True)
    assert live["executed"] is True

    # All 10 operational/HR/log tables wiped
    for tbl, _pk, _kind in mod.WIPE_TABLES:
        assert sb.store[tbl] == [], f"Expected {tbl} to be empty"

    # Inventory SKU preserved, qty/cost zeroed
    assert len(sb.store["inventory_items"]) == 1
    assert sb.store["inventory_items"][0]["on_hand_qty"] == 0.0
    assert sb.store["inventory_items"][0]["avg_unit_cost_inr"] == 0.0
    assert sb.store["inventory_items"][0]["last_purchase_rate_inr"] == 0.0

    # Customer & general_stock projects preserved, financial caches zeroed & contractor mistri stripped
    cust_prj = next(p for p in sb.store["projects"] if p["project_type"] == "customer")
    assert cust_prj["quoted_total"] == 2300000.0
    assert cust_prj["paid_cache_inr"] == 0.0
    assert cust_prj["cost_cache_inr"] == 0.0
    assert cust_prj["zoho_invoiced_cache"] == 0.0
    assert cust_prj["zoho_bills_cache"] == 0.0
    assert cust_prj["payment_stage"] == "advance_pending"
    assert cust_prj["estimator_ids"] == ["p-arvind"]
    assert cust_prj["current_owner_ids"] == ["p-shreyansh"]

    # Customers, vendors, salaried karigars kept; contractor_mistri removed; Rohit ensured
    assert len(sb.store["customers"]) == 1
    assert len(sb.store["vendors"]) == 1
    assert all(p["person_type"] != "contractor_mistri" for p in sb.store["people"])
    assert any(p["full_name"] == "Rohit" for p in sb.store["people"])
    assert any(p["full_name"] == "Arvind Ji" for p in sb.store["people"])

    # Exactly 6 active accounts configured; legacy accounts deactivated & zoho_account_id cleared
    active_accs = [a for a in sb.store["payment_accounts"] if a.get("is_active")]
    assert {a["code"] for a in active_accs} == {
        "cash_galla",
        "axis",
        "rgb",
        "ccrgb",
        "float_rohit",
        "float_kamlesh",
    }
    rohit_acc = next(a for a in active_accs if a["code"] == "float_rohit")
    assert rohit_acc["custodian_person_id"] is not None
    icici = next(a for a in sb.store["payment_accounts"] if a["code"] == "icici")
    assert icici["is_active"] is False
    assert icici["zoho_account_id"] is None


@pytest.mark.asyncio
async def test_reset_zoho_transactions_orders_deletes_and_skips_opening_balance():
    mod = _load_reset_module()
    req = httpx.Request("GET", "https://www.zohoapis.in/books/v3/test")
    calls: list[tuple[str, str]] = []

    async def fake_zoho_request(_client, method: str, path: str, **_kwargs):
        calls.append((method, path))
        if method == "GET" and path == "/bankaccounts":
            return httpx.Response(
                200,
                json={"bankaccounts": [{"account_id": "za-cash", "account_name": "Cash", "account_type": "cash", "balance": 5000}]},
                request=req,
            )
        if method == "DELETE":
            return httpx.Response(200, json={"code": 0}, request=req)
        return httpx.Response(200, json={"code": 0}, request=req)

    async def fake_list_all(_client, path: str, key: str, max_pages: int = 25):
        data_by_key = {
            "customerpayments": [{"payment_id": "cp-1"}],
            "vendorpayments": [{"payment_id": "vp-1"}],
            "creditnotes": [],
            "vendorcredits": [],
            "invoices": [{"invoice_id": "inv-1"}],
            "bills": [{"bill_id": "bill-1"}],
            "expenses": [{"expense_id": "exp-1"}],
            "banktransactions": [
                {"transaction_id": "bt-open", "transaction_type": "opening_balance"},
                {"transaction_id": "bt-1", "transaction_type": "expense"},
            ],
            "journals": [{"journal_id": "jrn-1"}],
        }
        return True, data_by_key.get(key, [])

    fake_settings = SimpleNamespace(zoho_enabled=True)
    with (
        patch.object(mod.zoho, "get_settings", return_value=fake_settings),
        patch.object(mod.zoho, "_zoho_request", side_effect=fake_zoho_request),
        patch.object(mod.zoho, "_list_all_zoho_docs", side_effect=fake_list_all),
        patch.object(mod.asyncio, "sleep", new=AsyncMock()),
    ):
        report = await mod.reset_zoho_transactions(execute=True)

    assert report["enabled"] is True
    delete_paths = [p for m, p in calls if m == "DELETE"]
    assert delete_paths == [
        "/customerpayments/cp-1",
        "/vendorpayments/vp-1",
        "/invoices/inv-1",
        "/bills/bill-1",
        "/expenses/exp-1",
        "/banktransactions/bt-1",
        "/journals/jrn-1",
    ]
    assert "/banktransactions/bt-open" not in delete_paths


def _load_opening_module():
    path = REPO_ROOT / "scripts" / "load_cutover_opening.py"
    spec = importlib.util.spec_from_file_location("load_cutover_opening", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_load_cutover_opening_dry_run_and_execute():
    from app.services.zoho.sync import _extract_party_from_desc

    mod = _load_opening_module()
    sb = _ResetFakeSupabase(
        {
            "projects": [
                {
                    "id": "prj-1",
                    "project_code": "PRJ-OCT-001",
                    "cf_project_code": "PRJ-OCT-001",
                    "customer_name": "Ambure Ji",
                    "project_type": "customer",
                    "quoted_total": 2000000.0,
                    "paid_cache_inr": 0.0,
                    "payment_stage": "advance_pending",
                },
                {
                    "id": "prj-2",
                    "project_code": "PRJ-OCT-003",
                    "cf_project_code": "PRJ-OCT-003",
                    "customer_name": "Aakash Prajapati",
                    "project_type": "customer",
                    "quoted_total": 1000000.0,
                    "paid_cache_inr": 0.0,
                    "payment_stage": "advance_pending",
                },
            ],
            "vendors": [
                {
                    "id": "v-1",
                    "canonical_name": "R P Wood",
                    "aliases": ["RP Wood"],
                    "zoho_contact_id": "zv-1",
                    "is_active": True,
                }
            ],
            "people": [
                {
                    "id": "p-arvind",
                    "full_name": "Arvind Ji",
                    "person_type": "salaried_karigar",
                    "aliases": ["Arvind"],
                    "monthly_salary_inr": 0.0,
                    "is_active": True,
                }
            ],
            "worker_payroll_entries": [],
        }
    )

    payload = {
        "cutover_date": "2026-11-23",
        "salary_month": "2026-11",
        "orders": [
            {"project_code": "PRJ-OCT-001", "quoted_total": 2300000, "paid_till_date_inr": 1500000},
            {"customer_name": "Aakash Prajapati", "quoted_total": 1021100, "paid_till_date_inr": 1021100},
        ],
        "vendor_udhaar": [
            {"vendor_name": "RP Wood", "amount_inr": 85000, "bill_no": "OPEN-RPWOOD", "notes": "Old balance"}
        ],
        "salaried_staff": [
            {"full_name": "Arvind", "monthly_salary_inr": 39000, "advance_paid_inr": 10000, "notes": "Nov adv"}
        ],
    }

    # 1. Dry-run makes zero mutations and zero Zoho requests
    dry_report = await mod.load_cutover_opening(sb, payload, execute=False)
    assert dry_report["executed"] is False
    assert sb.store["projects"][0]["paid_cache_inr"] == 0.0
    assert sb.store["people"][0]["monthly_salary_inr"] == 0.0
    assert sb.store["worker_payroll_entries"] == []

    # 2. Execute updates Supabase + posts Zoho Open AP Bill and Pre-Cutover Clearing Expense
    req = httpx.Request("POST", "https://www.zohoapis.in/books/v3/test")
    posted_bills: list[dict[str, Any]] = []
    posted_expenses: list[dict[str, Any]] = []
    coa_accounts = [
        {"account_id": "coa-sal", "account_name": "Salaries and Employee Wages", "account_type": "expense", "is_active": True}
    ]
    coa_seq = [1]

    async def fake_zoho_request(_client, method: str, path: str, *, json_body=None, **_kwargs):
        if method == "POST" and path == "/chartofaccounts":
            coa_seq[0] += 1
            new_id = f"coa-{coa_seq[0]}"
            acc = {"account_id": new_id, **json_body, "is_active": True}
            return httpx.Response(201, json={"chart_of_account": acc}, request=req)
        if method == "POST" and path == "/bills":
            posted_bills.append(json_body)
            return httpx.Response(201, json={"bill": {"bill_id": "zb-1", "status": "open"}}, request=req)
        if method == "POST" and path == "/expenses":
            posted_expenses.append(json_body)
            # Simulate Zoho rejecting equity as paid_through_account_id -> triggers Pre-Cutover Clearing fallback
            if json_body.get("paid_through_account_id") == "coa-2":
                return httpx.Response(400, json={"message": "Invalid paid_through_account_id"}, request=req)
            return httpx.Response(201, json={"expense": {"expense_id": "ze-1"}}, request=req)
        return httpx.Response(200, json={"code": 0}, request=req)

    fake_settings = SimpleNamespace(zoho_enabled=True)
    with (
        patch.object(mod.zoho, "get_settings", return_value=fake_settings),
        patch.object(mod.zoho, "_get_chart_of_accounts", new=AsyncMock(return_value=coa_accounts)),
        patch.object(mod.zoho, "_find_existing_by_reference", new=AsyncMock(return_value=None)),
        patch.object(
            mod.zoho,
            "resolve_payee_zoho_vendor",
            new=AsyncMock(return_value={"contact_id": "zv-1", "canonical_name": "R P Wood"}),
        ),
        patch.object(mod.zoho, "_zoho_request", side_effect=fake_zoho_request),
    ):
        live_report = await mod.load_cutover_opening(sb, payload, execute=True)

    assert live_report["executed"] is True
    assert sb.store["projects"][0]["quoted_total"] == 2300000.0
    assert sb.store["projects"][0]["paid_cache_inr"] == 1500000.0
    assert sb.store["projects"][0]["payment_stage"] == "partial_paid"
    assert sb.store["projects"][1]["payment_stage"] == "payment_complete"

    assert len(posted_bills) == 1
    assert posted_bills[0]["status"] == "open"
    assert posted_bills[0]["reference_number"].startswith("cutover_ap_")
    assert posted_bills[0]["line_items"][0]["account_id"] == "coa-2"

    assert sb.store["people"][0]["monthly_salary_inr"] == 39000.0
    assert len(sb.store["worker_payroll_entries"]) == 1
    pe = sb.store["worker_payroll_entries"][0]
    assert pe["payment_account_id"] is None
    assert pe["entry_type"] == "advance"
    assert pe["zoho_txn_id"] == "ze-1"
    assert pe["zoho_reference_number"].startswith("cutover_adv_2026_11_")

    assert len(posted_expenses) == 2  # 1st rejected equity -> 2nd succeeded on Pre-Cutover Clearing
    final_exp = posted_expenses[-1]
    assert final_exp["account_id"] == "coa-sal"
    assert final_exp["paid_through_account_id"] == "coa-3"
    assert _extract_party_from_desc(final_exp["description"]) == "Arvind Ji"


@pytest.mark.asyncio
async def test_reset_zoho_transactions_zero_opening_clears_contacts_and_ob():
    mod = _load_reset_module()
    req = httpx.Request("GET", "https://www.zohoapis.in/books/v3/test")
    calls: list[tuple[str, str, Any]] = []
    ob_deleted = [False]

    async def fake_zoho_request(_client, method: str, path: str, *, json_body=None, **_kwargs):
        calls.append((method, path, json_body))
        if method == "GET" and path == "/bankaccounts":
            bal = 0.0 if ob_deleted[0] else 85000.0
            return httpx.Response(
                200,
                json={"bankaccounts": [{"account_id": "za-rgb", "account_name": "RGB", "account_type": "bank", "balance": bal}]},
                request=req,
            )
        if method == "GET" and path == "/settings/openingbalances":
            return httpx.Response(
                200,
                json={"opening_balance": {"opening_balance_id": "ob-1", "date": "2026-11-21", "accounts": []}},
                request=req,
            )
        if method == "DELETE" and path == "/settings/openingbalances":
            ob_deleted[0] = True
            return httpx.Response(200, json={"code": 0}, request=req)
        if method == "GET" and path == "/contacts/c-1":
            return httpx.Response(
                200,
                json={"contact": {"contact_id": "c-1", "contact_name": "RAJASTHAN PLASTIC"}},
                request=req,
            )
        if method == "PUT" and path == "/contacts/c-1":
            return httpx.Response(200, json={"code": 0}, request=req)
        return httpx.Response(200, json={"code": 0}, request=req)

    async def fake_list_all(_client, path: str, key: str, max_pages: int = 25):
        if key == "contacts":
            return True, [{"contact_id": "c-1", "contact_name": "RAJASTHAN PLASTIC", "outstanding_payable_amount": 31236.0, "outstanding_receivable_amount": 0.0}]
        if key == "banktransactions":
            return True, [{"transaction_id": "ob-1", "transaction_type": "opening_balance"}]
        return True, []

    fake_settings = SimpleNamespace(zoho_enabled=True)
    with (
        patch.object(mod.zoho, "get_settings", return_value=fake_settings),
        patch.object(mod.zoho, "_zoho_request", side_effect=fake_zoho_request),
        patch.object(mod.zoho, "_list_all_zoho_docs", side_effect=fake_list_all),
        patch.object(mod.asyncio, "sleep", new=AsyncMock()),
    ):
        report = await mod.reset_zoho_transactions(execute=True, zero_opening=True)

    assert report["opening_cleared"]["deleted_via_settings"] is True
    assert report["opening_cleared"]["contacts_cleared"] == 1
    assert any(m == "PUT" and p == "/contacts/c-1" for m, p, _b in calls)
    assert any(m == "DELETE" and p == "/settings/openingbalances" for m, p, _b in calls)
    assert report["account_mapping"]["matched"][0]["current_balance"] == 0.0


@pytest.mark.asyncio
async def test_zoho_get_access_token_uses_form_data_and_redacts_secrets_on_error():
    from app.config import Settings
    from app.services import zoho

    captured: list[dict[str, Any]] = []
    secret_refresh = "1000.SECRET_REFRESH_TOKEN"
    secret_client = "SECRET_CLIENT_SECRET"

    async def fake_post(self, url, **kwargs):
        captured.append({"url": url, "kwargs": kwargs})
        return httpx.Response(
            400,
            request=httpx.Request("POST", url),
            json={"error": "Access Denied"},
        )

    fake_cfg = Settings(
        _env_file=None,
        zoho_org_id="org-1",
        zoho_client_id="cid-1",
        zoho_client_secret=secret_client,
        zoho_refresh_token=secret_refresh,
    )
    prev_cache = dict(zoho._token_cache)
    zoho._token_cache.update({"access_token": None, "expires_at": 0.0})
    try:
        with (
            patch("app.services.zoho.get_settings", return_value=fake_cfg),
            patch("httpx.AsyncClient.post", new=fake_post),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await zoho._get_access_token(force_refresh=True)
    finally:
        zoho._token_cache.update(prev_cache)

    assert len(captured) == 1
    assert "params" not in captured[0]["kwargs"]
    assert captured[0]["kwargs"]["data"]["refresh_token"] == secret_refresh
    assert captured[0]["kwargs"]["data"]["client_secret"] == secret_client
    err_str = str(exc_info.value)
    assert "Access Denied" in err_str
    assert secret_refresh not in err_str
    assert secret_client not in err_str


@pytest.mark.asyncio
async def test_apply_account_opening_balances_debit_credit_and_pulse_sync():
    mod = _load_opening_module()
    sb = _ResetFakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "a-cash",
                    "code": "cash_galla",
                    "display_name": "Cash",
                    "account_kind": "cash_galla",
                    "aliases": ["cash", "cash galla"],
                    "zoho_account_id": "za-cash",
                    "cached_balance_inr": 0.0,
                    "is_active": True,
                    "sort_order": 1,
                },
                {
                    "id": "a-rgb",
                    "code": "rgb",
                    "display_name": "RGB",
                    "account_kind": "bank",
                    "aliases": ["rgb"],
                    "zoho_account_id": "za-rgb",
                    "cached_balance_inr": 0.0,
                    "is_active": True,
                    "sort_order": 3,
                },
                {
                    "id": "a-ccrgb",
                    "code": "ccrgb",
                    "display_name": "CCRGB",
                    "account_kind": "od_cc",
                    "aliases": ["ccrgb", "cc rgb"],
                    "zoho_account_id": "za-ccrgb",
                    "cached_balance_inr": 0.0,
                    "is_active": True,
                    "sort_order": 4,
                },
            ]
        }
    )

    balances = [
        {"account": "cash_galla", "balance_inr": 130000},
        {"account": "rgb", "balance_inr": 42469.68},
        {"account": "ccrgb", "balance_inr": -1421360},
    ]

    # Dry-run makes zero Zoho writes and keeps Supabase at 0.0
    dry = await mod.apply_account_opening_balances(sb, balances, cutover_date="2026-11-23", execute=False)
    assert dry["processed"] == 3
    assert dry["updated"] == 0
    assert sb.store["payment_accounts"][0]["cached_balance_inr"] == 0.0

    req = httpx.Request("POST", "https://www.zohoapis.in/books/v3/settings/openingbalances")
    posted_ob: list[tuple[str, dict[str, Any]]] = []
    zoho_accs = [
        {"account_id": "za-cash", "account_name": "Cash Galla", "account_type": "cash", "currency_id": "cur-inr", "balance": 0.0, "is_active": True},
        {"account_id": "za-rgb", "account_name": "RGB", "account_type": "bank", "currency_id": "cur-inr", "balance": 0.0, "is_active": True},
        {"account_id": "za-ccrgb", "account_name": "CCRGB", "account_type": "bank", "currency_id": "cur-inr", "balance": 0.0, "is_active": True},
    ]

    async def fake_zoho_request(_client, method: str, path: str, *, json_body=None, **_kwargs):
        if method == "GET" and path == "/bankaccounts":
            return httpx.Response(200, json={"bankaccounts": zoho_accs}, request=req)
        if method == "GET" and path == "/settings/openingbalances":
            return httpx.Response(200, json={"opening_balance": {}}, request=req)
        if method in ("POST", "PUT") and path == "/settings/openingbalances":
            posted_ob.append((method, json_body))
            # Reflect opening balances on zoho_accs for sync_owner_pulse_snapshot
            by_id = {a["account_id"]: a for a in zoho_accs}
            for row in json_body["accounts"]:
                val = float(row["bcy_amount"])
                by_id[row["account_id"]]["balance"] = -val if row["debit_or_credit"] == "credit" else val
            return httpx.Response(200, json={"code": 0}, request=req)
        return httpx.Response(200, json={"code": 0}, request=req)

    fake_settings = SimpleNamespace(zoho_enabled=True)
    with (
        patch.object(mod.zoho, "get_settings", return_value=fake_settings),
        patch.object(mod.zoho, "_zoho_request", side_effect=fake_zoho_request),
        patch.object(mod.zoho, "_list_all_zoho_docs", new=AsyncMock(return_value=(True, []))),
    ):
        live = await mod.apply_account_opening_balances(sb, balances, cutover_date="2026-11-23", execute=True)

    assert live["updated"] == 3
    assert live["pulse_synced"] == 3
    assert len(posted_ob) == 1
    method, body = posted_ob[0]
    assert method == "POST"
    assert body["date"] == "2026-11-23"
    by_zid = {r["account_id"]: r for r in body["accounts"]}
    assert by_zid["za-cash"] == {"account_id": "za-cash", "debit_or_credit": "debit", "bcy_amount": 130000.0, "amount": 130000.0, "currency_id": "cur-inr", "exchange_rate": 1.0}
    assert by_zid["za-rgb"] == {"account_id": "za-rgb", "debit_or_credit": "debit", "bcy_amount": 42469.68, "amount": 42469.68, "currency_id": "cur-inr", "exchange_rate": 1.0}
    assert by_zid["za-ccrgb"] == {"account_id": "za-ccrgb", "debit_or_credit": "credit", "bcy_amount": 1421360.0, "amount": 1421360.0, "currency_id": "cur-inr", "exchange_rate": 1.0}

    cached_by_code = {a["code"]: a["cached_balance_inr"] for a in sb.store["payment_accounts"]}
    assert cached_by_code == {"cash_galla": 130000.0, "rgb": 42469.68, "ccrgb": -1421360.0}




