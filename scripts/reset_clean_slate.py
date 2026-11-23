"""Clean-slate financial, HR/attendance, inventory ledger, and Zoho Books reset.

Preserves master directories (customers, vendors, operators, salaried staff) and
Production orders/items while zeroing order financial caches, wiping Zoho Books
transactions, wiping HR/attendance/log tables, and configuring the 6 active accounts:
  - Banks / OD (3): Axis Bank, RGB, CCRGB
  - Cash & Wallets (3): Cash, Rohit Wallet, Kamlesh Wallet

Defaults safely to `--dry-run`. Pass `--execute` to perform live mutations.

Usage:
    .\\backend\\.venv\\Scripts\\python.exe scripts\\reset_clean_slate.py --dry-run
    .\\backend\\.venv\\Scripts\\python.exe scripts\\reset_clean_slate.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zoho_cli_cache import load_cli_zoho_token_cache, save_cli_zoho_token_cache  # noqa: E402
from app.db import get_supabase_admin  # noqa: E402
from app.services import zoho  # noqa: E402

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
MIN_BIGINT = -9000000000000000000

# Strict FK-safe wipe order: children / logs first.
WIPE_TABLES: list[tuple[str, str, str]] = [
    ("inventory_movements", "id", "uuid"),
    ("worker_payroll_entries", "id", "uuid"),
    ("monthly_fnf_sheets", "id", "uuid"),
    ("worker_attendance", "id", "uuid"),
    ("cash_ledger", "id", "uuid"),
    ("agent_goals", "id", "uuid"),
    ("telegram_state", "id", "uuid"),
    ("telegram_inbox_buffer", "id", "uuid"),
    ("telegram_webhook_dedup", "telegram_update_id", "bigint"),
    ("followups", "id", "uuid"),
]

TARGET_ACTIVE_ACCOUNTS: list[dict[str, Any]] = [
    {
        "code": "cash_galla",
        "display_name": "Cash",
        "account_kind": "cash_galla",
        "aliases": ["cash", "cash galla", "galla", "shop cash", "nakad"],
        "custodian_names": (),
        "sort_order": 1,
    },
    {
        "code": "axis",
        "display_name": "Axis Bank",
        "account_kind": "bank",
        "aliases": ["axis", "axis bank"],
        "custodian_names": (),
        "sort_order": 2,
    },
    {
        "code": "rgb",
        "display_name": "RGB",
        "account_kind": "bank",
        "aliases": ["rgb", "rajasthan gramin bank", "gramin bank"],
        "custodian_names": (),
        "sort_order": 3,
    },
    {
        "code": "ccrgb",
        "display_name": "CCRGB",
        "account_kind": "od_cc",
        "aliases": ["ccrgb", "cc rgb", "od"],
        "custodian_names": (),
        "sort_order": 4,
    },
    {
        "code": "float_rohit",
        "display_name": "Rohit Wallet",
        "account_kind": "supervisor_float",
        "aliases": ["rohit", "rohit wallet", "rohit float", "rohit cash", "rohit ji", "rohit bhaiya"],
        "custodian_names": ("rohit",),
        "sort_order": 5,
    },
    {
        "code": "float_kamlesh",
        "display_name": "Kamlesh Wallet",
        "account_kind": "supervisor_float",
        "aliases": ["kamlesh", "kamlesh wallet", "kamlesh float", "kamlesh cash", "kamlesh bhaiya", "kamlesh ji"],
        "custodian_names": ("kamlesh", "kamlesh bhaiya"),
        "sort_order": 6,
    },
]
TARGET_ACTIVE_CODES = {a["code"] for a in TARGET_ACTIVE_ACCOUNTS}

# Strict FK-safe Zoho Books wipe order: child payments/credits before invoices/bills, then bank/journals.
ZOHO_DELETE_MODULES: list[dict[str, Any]] = [
    {"path": "/customerpayments", "key": "customerpayments", "id_field": "payment_id", "void_fallback": False},
    {"path": "/vendorpayments", "key": "vendorpayments", "id_field": "payment_id", "void_fallback": False},
    {"path": "/creditnotes", "key": "creditnotes", "id_field": "creditnote_id", "void_fallback": False},
    {"path": "/vendorcredits", "key": "vendorcredits", "id_field": "vendor_credit_id", "void_fallback": False},
    {"path": "/invoices", "key": "invoices", "id_field": "invoice_id", "void_fallback": True},
    {"path": "/bills", "key": "bills", "id_field": "bill_id", "void_fallback": True},
    {"path": "/expenses", "key": "expenses", "id_field": "expense_id", "void_fallback": False},
    {"path": "/banktransactions", "key": "banktransactions", "id_field": "transaction_id", "void_fallback": False},
    {"path": "/journals", "key": "journals", "id_field": "journal_id", "void_fallback": False},
]


def _is_missing_table_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "pgrst205" in msg or "42p01" in msg or "could not find the table" in msg or "does not exist" in msg


def _safe_select_all(sb: Any, table: str, columns: str = "*") -> tuple[list[dict[str, Any]], bool]:
    """Select all rows from a Supabase table, returning (rows, table_exists)."""
    try:
        rows = sb.table(table).select(columns).execute().data or []
        return list(rows), True
    except Exception as exc:
        if _is_missing_table_error(exc):
            return [], False
        raise


def _wipe_table_rows(sb: Any, table: str, pk_col: str, pk_kind: str) -> None:
    """Delete all rows from a table using explicit PostgREST filters."""
    q = sb.table(table).delete()
    if pk_kind == "bigint":
        q = q.gte(pk_col, MIN_BIGINT)
    else:
        q = q.neq(pk_col, ZERO_UUID)
    q.execute()


def _resolve_custodian_id(people_rows: list[dict[str, Any]], custodian_names: tuple[str, ...]) -> str | None:
    if not custodian_names:
        return None
    wanted = {n.strip().lower() for n in custodian_names}
    for row in people_rows:
        if str(row.get("full_name") or "").strip().lower() in wanted:
            return str(row["id"])
    return None


def _ensure_rohit_person(sb: Any, people_rows: list[dict[str, Any]], *, execute: bool) -> list[dict[str, Any]]:
    has_rohit = any(str(r.get("full_name") or "").strip().lower() == "rohit" for r in people_rows)
    if has_rohit or not execute:
        return people_rows
    res = (
        sb.table("people")
        .insert(
            {
                "full_name": "Rohit",
                "person_type": "operator",
                "role_tags": ["supervisor", "float_custodian"],
                "aliases": ["Rohit Ji", "Rohit Bhaiya"],
                "is_active": True,
            }
        )
        .execute()
        .data
        or []
    )
    return [*people_rows, *res]


def _configure_six_accounts(
    sb: Any, existing_accounts: list[dict[str, Any]], people_rows: list[dict[str, Any]], *, execute: bool
) -> dict[str, Any]:
    """Configure the exact 6 active accounts and deactivate all legacy accounts."""
    by_code = {str(a.get("code") or "").strip().lower(): a for a in existing_accounts if a.get("code")}
    deactivated_codes = [
        str(a.get("code"))
        for a in existing_accounts
        if str(a.get("code") or "").strip().lower() not in TARGET_ACTIVE_CODES
        and a.get("is_active") is not False
    ]

    if execute:
        # Clear deactivated rows first (including zoho_account_id) so UNIQUE zoho_account_id never collides.
        for acc in existing_accounts:
            code = str(acc.get("code") or "").strip().lower()
            if code not in TARGET_ACTIVE_CODES and acc.get("id"):
                sb.table("payment_accounts").update(
                    {
                        "is_active": False,
                        "cached_balance_inr": 0.0,
                        "balance_synced_at": None,
                        "zoho_account_id": None,
                    }
                ).eq("id", acc["id"]).execute()

        for spec in TARGET_ACTIVE_ACCOUNTS:
            custodian_id = _resolve_custodian_id(people_rows, spec["custodian_names"])
            payload = {
                "code": spec["code"],
                "display_name": spec["display_name"],
                "account_kind": spec["account_kind"],
                "aliases": list(spec["aliases"]),
                "custodian_person_id": custodian_id,
                "is_active": True,
                "sort_order": spec["sort_order"],
                "cached_balance_inr": 0.0,
                "balance_synced_at": None,
            }
            cur = by_code.get(spec["code"])
            if cur and cur.get("id"):
                sb.table("payment_accounts").update(payload).eq("id", cur["id"]).execute()
            else:
                sb.table("payment_accounts").insert(payload).execute()

    return {
        "target_active": [a["display_name"] for a in TARGET_ACTIVE_ACCOUNTS],
        "to_deactivate_currently_active": deactivated_codes,
        "rohit_present": any(str(r.get("full_name") or "").strip().lower() == "rohit" for r in people_rows),
    }


def reset_supabase_data(sb: Any, *, execute: bool) -> dict[str, Any]:
    """Inspect and optionally reset Supabase operational/financial/HR data cleanly."""
    wiped_counts: dict[str, int] = {}
    missing_tables: list[str] = []

    for tbl, pk_col, pk_kind in WIPE_TABLES:
        rows, exists = _safe_select_all(sb, tbl, pk_col)
        if not exists:
            missing_tables.append(tbl)
            continue
        wiped_counts[tbl] = len(rows)
        if execute and rows:
            _wipe_table_rows(sb, tbl, pk_col, pk_kind)

    inv_items, inv_exists = _safe_select_all(sb, "inventory_items", "id,on_hand_qty,avg_unit_cost_inr")
    if not inv_exists:
        missing_tables.append("inventory_items")
    elif execute and inv_items:
        sb.table("inventory_items").update(
            {
                "on_hand_qty": 0.0,
                "avg_unit_cost_inr": 0.0,
                "last_purchase_rate_inr": 0.0,
            }
        ).neq("id", ZERO_UUID).execute()

    projects_rows, prj_exists = _safe_select_all(
        sb,
        "projects",
        "id,project_type,paid_cache_inr,cost_cache_inr,zoho_invoiced_cache,zoho_bills_cache,payment_stage,estimator_ids,current_owner_ids",
    )
    customer_projects = [p for p in projects_rows if p.get("project_type") == "customer"]
    gen_stock_projects = [p for p in projects_rows if p.get("project_type") == "general_stock"]

    if execute and prj_exists:
        if customer_projects:
            sb.table("projects").update(
                {
                    "paid_cache_inr": 0.0,
                    "cost_cache_inr": 0.0,
                    "zoho_invoiced_cache": 0.0,
                    "zoho_bills_cache": 0.0,
                    "payment_stage": "advance_pending",
                    "zoho_synced_at": None,
                }
            ).eq("project_type", "customer").execute()
        if gen_stock_projects:
            sb.table("projects").update(
                {
                    "paid_cache_inr": 0.0,
                    "cost_cache_inr": 0.0,
                    "zoho_invoiced_cache": 0.0,
                    "zoho_bills_cache": 0.0,
                    "zoho_synced_at": None,
                }
            ).eq("project_type", "general_stock").execute()

    people_rows, _ = _safe_select_all(sb, "people", "id,full_name,person_type,is_active")
    contractor_mistris = [p for p in people_rows if p.get("person_type") == "contractor_mistri"]
    contractor_ids = {str(p["id"]) for p in contractor_mistris if p.get("id")}

    if execute and contractor_ids:
        for prj in projects_rows:
            est = list(prj.get("estimator_ids") or [])
            owners = list(prj.get("current_owner_ids") or [])
            new_est = [pid for pid in est if str(pid) not in contractor_ids]
            new_owners = [pid for pid in owners if str(pid) not in contractor_ids]
            if len(new_est) != len(est) or len(new_owners) != len(owners):
                sb.table("projects").update(
                    {"estimator_ids": new_est, "current_owner_ids": new_owners}
                ).eq("id", prj["id"]).execute()

        sb.table("people").delete().eq("person_type", "contractor_mistri").execute()
        people_rows = [p for p in people_rows if p.get("person_type") != "contractor_mistri"]

    people_rows = _ensure_rohit_person(sb, people_rows, execute=execute)
    existing_accounts, _ = _safe_select_all(sb, "payment_accounts", "*")
    accounts_summary = _configure_six_accounts(sb, existing_accounts, people_rows, execute=execute)

    customers_rows, _ = _safe_select_all(sb, "customers", "id,is_active")
    vendors_rows, _ = _safe_select_all(sb, "vendors", "id,is_active")
    items_rows, _ = _safe_select_all(sb, "project_items", "id")

    remaining_people = [p for p in people_rows if p.get("person_type") != "contractor_mistri"]
    return {
        "executed": execute,
        "missing_tables": sorted(set(missing_tables)),
        "wiped_tables": wiped_counts,
        "inventory_items_zeroed": len(inv_items) if inv_exists else 0,
        "customer_orders_zeroed": len(customer_projects),
        "general_stock_zeroed": len(gen_stock_projects),
        "contractor_mistris_removed": [p.get("full_name") for p in contractor_mistris],
        "accounts": accounts_summary,
        "preserved": {
            "customers": len(customers_rows),
            "vendors": len(vendors_rows),
            "people_kept": len(remaining_people),
            "orders": len(customer_projects),
            "order_items": len(items_rows),
        },
    }


async def _zoho_delete_with_retry(
    client: httpx.AsyncClient, path: str, *, allow_void_fallback: bool = False, void_base: str = ""
) -> tuple[bool, str]:
    """DELETE a Zoho Books doc with single 429 backoff and optional void+delete fallback."""
    resp = await zoho._zoho_request(client, "DELETE", path)
    if resp.status_code == 429:
        await asyncio.sleep(1.5)
        resp = await zoho._zoho_request(client, "DELETE", path)

    if resp.is_success or resp.status_code == 404:
        return True, "already_missing" if resp.status_code == 404 else "deleted"

    if allow_void_fallback and void_base:
        v_resp = await zoho._zoho_request(client, "POST", f"{void_base}/status/void", json_body={})
        if v_resp.status_code == 429:
            await asyncio.sleep(1.5)
            v_resp = await zoho._zoho_request(client, "POST", f"{void_base}/status/void", json_body={})
        if v_resp.is_success:
            d_resp = await zoho._zoho_request(client, "DELETE", path)
            if d_resp.is_success or d_resp.status_code == 404:
                return True, "voided_and_deleted"
            return True, "voided"

    return False, f"HTTP {resp.status_code}: {resp.text[:140]}"


def _inspect_zoho_account_mapping(zoho_accs: list[dict[str, Any]]) -> dict[str, Any]:
    claimed: set[str] = set()
    matched: list[dict[str, Any]] = []
    missing: list[str] = []

    for spec in TARGET_ACTIVE_ACCOUNTS:
        hit = zoho._pick_matching_zoho_bank_account(spec, zoho_accs, claimed)
        if hit and hit.get("account_id"):
            zid = str(hit["account_id"])
            claimed.add(zid)
            matched.append(
                {
                    "local_code": spec["code"],
                    "local_name": spec["display_name"],
                    "zoho_name": hit.get("account_name"),
                    "zoho_type": hit.get("account_type"),
                    "current_balance": round(float(hit.get("balance") or 0), 2),
                }
            )
        else:
            missing.append(spec["display_name"])

    return {"matched": matched, "missing": missing}


async def _clear_contact_opening_balances(
    client: httpx.AsyncClient, warnings: list[str]
) -> int:
    """Clear any Zoho Contact-level opening receivable/payable balances."""
    ok, contacts = await zoho._list_all_zoho_docs(client, "/contacts", "contacts", max_pages=25)
    if not ok:
        warnings.append("GET /contacts failed while checking opening balances")
        return 0

    cleared = 0
    for c in contacts:
        cid = str(c.get("contact_id") or "").strip()
        if not cid:
            continue
        recv = abs(float(c.get("outstanding_receivable_amount") or 0))
        pay = abs(float(c.get("outstanding_payable_amount") or 0))
        if recv < 0.01 and pay < 0.01:
            continue

        det_resp = await zoho._zoho_request(client, "GET", f"/contacts/{cid}")
        c_obj = (det_resp.json().get("contact") or {}) if det_resp.is_success else c
        cname = str(c_obj.get("contact_name") or c.get("contact_name") or "").strip()
        upd_resp = await zoho._zoho_request(
            client,
            "PUT",
            f"/contacts/{cid}",
            json_body={
                "contact_name": cname,
                "opening_balances": [],
                "opening_balance_amount": 0,
            },
        )
        if not upd_resp.is_success:
            upd_resp = await zoho._zoho_request(
                client,
                "PUT",
                f"/contacts/{cid}",
                json_body={"contact_name": cname, "opening_balances": []},
            )
        if upd_resp.is_success:
            cleared += 1
        else:
            warnings.append(
                f"PUT /contacts/{cid} ({cname}) failed ({upd_resp.status_code}): {upd_resp.text[:140]}"
            )
        await asyncio.sleep(0.15)
    return cleared


async def _zero_zoho_opening_balances(
    client: httpx.AsyncClient,
    opening_txn_ids: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Clear Zoho Organization & Contact Opening Balances so all Bank/Cash/Contact balances hit 0.00."""
    ob_resp = await zoho._zoho_request(client, "GET", "/settings/openingbalances")
    ob_obj = (ob_resp.json().get("opening_balance") or {}) if ob_resp.is_success else {}
    ob_id = str(ob_obj.get("opening_balance_id") or "").strip()

    # 1) Try deleting organization opening balances directly first.
    del_resp = await zoho._zoho_request(client, "DELETE", "/settings/openingbalances")
    contacts_cleared = await _clear_contact_opening_balances(client, warnings)

    if not del_resp.is_success and del_resp.status_code != 404:
        del_resp = await zoho._zoho_request(client, "DELETE", "/settings/openingbalances")

    # 2) Fallback: DELETE /settings/openingbalances/{id}
    if not del_resp.is_success and del_resp.status_code != 404 and ob_id:
        del_resp = await zoho._zoho_request(client, "DELETE", f"/settings/openingbalances/{ob_id}")

    # 3) Fallback: PUT /settings/openingbalances with zeroed accounts
    if not del_resp.is_success and del_resp.status_code != 404 and ob_obj:
        zeroed_groups: list[dict[str, Any]] = []
        for grp in ob_obj.get("accounts") or []:
            acct_list = grp.get("accounts_list") or []
            if not acct_list:
                continue
            zeroed_groups.append(
                {
                    "account_type": grp.get("account_type"),
                    "accounts_list": [
                        {
                            "account_id": a.get("account_id"),
                            "debit_or_credit": a.get("debit_or_credit") or "debit",
                            "bcy_amount": 0,
                            "amount": 0,
                        }
                        for a in acct_list
                        if a.get("account_id")
                    ],
                }
            )
        put_resp = await zoho._zoho_request(
            client,
            "PUT",
            "/settings/openingbalances",
            json_body={
                "date": ob_obj.get("date") or "2026-11-01",
                "accounts": zeroed_groups,
            },
        )
        if not put_resp.is_success:
            warnings.append(
                f"PUT /settings/openingbalances failed ({put_resp.status_code}): {put_resp.text[:140]}"
            )

    # 4) Fallback: delete any remaining opening_balance banktransactions directly if balances remain non-zero
    ba_check = await zoho._zoho_request(client, "GET", "/bankaccounts")
    remaining_nonzero = [
        b
        for b in ((ba_check.json().get("bankaccounts") or []) if ba_check.is_success else [])
        if abs(float(b.get("balance") or 0)) >= 0.01
    ]
    if remaining_nonzero:
        candidate_ids = list(dict.fromkeys([*opening_txn_ids, *([ob_id] if ob_id else [])]))
        for tid in candidate_ids:
            bt_resp = await zoho._zoho_request(client, "DELETE", f"/banktransactions/{tid}")
            if not bt_resp.is_success and bt_resp.status_code != 404:
                warnings.append(
                    f"DELETE /banktransactions/{tid} failed ({bt_resp.status_code}): {bt_resp.text[:140]}"
                )

    return {
        "opening_balance_id": ob_id or None,
        "contacts_cleared": contacts_cleared,
        "deleted_via_settings": bool(del_resp.is_success or del_resp.status_code == 404),
    }


async def reset_zoho_transactions(*, execute: bool, zero_opening: bool = False) -> dict[str, Any]:
    """Inspect Zoho Books India accounts/transactions and optionally wipe all transactions."""
    settings = zoho.get_settings()
    if not settings.zoho_enabled:
        return {"enabled": False, "executed": execute, "reason": "Zoho credentials not configured"}

    modules_summary: list[dict[str, Any]] = []
    warnings: list[str] = []
    account_mapping: dict[str, Any] = {"matched": [], "missing": []}
    opening_cleared_info: dict[str, Any] | None = None
    opening_txn_ids: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            ba_resp = await zoho._zoho_request(client, "GET", "/bankaccounts")
            if ba_resp.is_success:
                zoho_accs = ba_resp.json().get("bankaccounts") or []
                account_mapping = _inspect_zoho_account_mapping(zoho_accs)
            else:
                warnings.append(f"/bankaccounts returned HTTP {ba_resp.status_code}")

            for mod in ZOHO_DELETE_MODULES:
                path = mod["path"]
                key = mod["key"]
                id_field = mod["id_field"]
                ok, rows = await zoho._list_all_zoho_docs(client, path, key, max_pages=25)
                if not ok:
                    warnings.append(f"GET {path} unavailable/unauthorized (skipped)")
                    modules_summary.append({"module": key, "found": 0, "skipped_opening": 0, "deleted": 0, "failed": 0, "warning": True})
                    continue

                skipped_opening = 0
                targets: list[str] = []
                for row in rows:
                    doc_id = str(row.get(id_field) or "").strip()
                    if not doc_id:
                        continue
                    if key == "banktransactions" and str(row.get("transaction_type") or "").strip().lower() == "opening_balance":
                        skipped_opening += 1
                        if doc_id not in opening_txn_ids:
                            opening_txn_ids.append(doc_id)
                        continue
                    targets.append(doc_id)

                deleted = 0
                failed = 0
                if execute and targets:
                    for doc_id in targets:
                        doc_path = f"{path}/{doc_id}"
                        ok_del, note = await _zoho_delete_with_retry(
                            client,
                            doc_path,
                            allow_void_fallback=bool(mod["void_fallback"]),
                            void_base=doc_path,
                        )
                        if ok_del:
                            deleted += 1
                        else:
                            failed += 1
                            warnings.append(f"DELETE {doc_path}: {note}")
                        await asyncio.sleep(0.15)

                modules_summary.append(
                    {
                        "module": key,
                        "found": len(targets),
                        "skipped_opening": skipped_opening,
                        "deleted": deleted,
                        "failed": failed,
                        "warning": False,
                    }
                )

            if execute and zero_opening:
                opening_cleared_info = await _zero_zoho_opening_balances(
                    client, opening_txn_ids, warnings
                )
                ba_after = await zoho._zoho_request(client, "GET", "/bankaccounts")
                if ba_after.is_success:
                    account_mapping = _inspect_zoho_account_mapping(
                        ba_after.json().get("bankaccounts") or []
                    )
    except httpx.HTTPError as exc:
        return {"enabled": True, "executed": execute, "error": str(exc)}

    return {
        "enabled": True,
        "executed": execute,
        "zero_opening": zero_opening,
        "opening_cleared": opening_cleared_info,
        "account_mapping": account_mapping,
        "modules": modules_summary,
        "warnings": warnings,
    }


def _print_report(sb_report: dict[str, Any] | None, zoho_report: dict[str, Any] | None, *, execute: bool) -> None:
    mode = "LIVE EXECUTE" if execute else "DRY-RUN PREVIEW (no changes made)"
    print(f"\n=== Workshop Munim OS Clean-Slate Reset [{mode}] ===\n")

    if sb_report is not None:
        print("1) SUPABASE:")
        if sb_report.get("missing_tables"):
            print(f"   - Missing tables (unapplied migrations?): {', '.join(sb_report['missing_tables'])}")
        print("   - Tables to wipe / wiped:")
        for tbl, cnt in sb_report["wiped_tables"].items():
            print(f"       * {tbl}: {cnt} rows")
        print(f"   - inventory_items financial/qty reset to 0: {sb_report['inventory_items_zeroed']} SKUs")
        print(f"   - customer orders financial caches zeroed: {sb_report['customer_orders_zeroed']} orders")
        print(f"   - general_stock rows zeroed: {sb_report['general_stock_zeroed']} rows")
        removed = sb_report["contractor_mistris_removed"]
        print(f"   - contractor_mistri removed from people ({len(removed)}): {', '.join(removed) if removed else 'none'}")
        acc = sb_report["accounts"]
        print(f"   - target 6 active accounts: {', '.join(acc['target_active'])}")
        deact = acc["to_deactivate_currently_active"]
        print(f"   - legacy accounts to deactivate ({len(deact)}): {', '.join(deact) if deact else 'none'}")
        pres = sb_report["preserved"]
        print(
            "   - preserved master/production rows: "
            f"customers={pres['customers']}, vendors={pres['vendors']}, "
            f"people={pres['people_kept']}, orders={pres['orders']}, items={pres['order_items']}"
        )

    if zoho_report is not None:
        print("\n2) ZOHO BOOKS INDIA:")
        if not zoho_report.get("enabled"):
            print(f"   - Skipped: {zoho_report.get('reason', 'not enabled')}")
        elif zoho_report.get("error"):
            print(f"   - ERROR: {zoho_report['error']}")
        else:
            amap = zoho_report.get("account_mapping") or {}
            print("   - 6-Account Zoho /bankaccounts Mapping Preview:")
            for m in amap.get("matched", []):
                print(
                    f"       [OK] {m['local_name']} ({m['local_code']}) -> "
                    f"Zoho '{m['zoho_name']}' [{m['zoho_type']}] (current bal: {m['current_balance']:,.2f})"
                )
            for miss in amap.get("missing", []):
                print(f"       [MISSING IN ZOHO] {miss} -> create this Cash/Bank account in Zoho Books Banking")
            if zoho_report.get("opening_cleared"):
                oc = zoho_report["opening_cleared"]
                print(
                    f"   - Opening balances zeroed: deleted_via_settings={oc.get('deleted_via_settings')}, "
                    f"contacts_cleared={oc.get('contacts_cleared')}"
                )
            if zoho_report.get("pulse_sync"):
                ps = zoho_report["pulse_sync"]
                print(f"   - Supabase payment_accounts synced: accounts_synced={ps.get('accounts_synced', 0)}")
            print("   - Transactions by module:")
            for mod in zoho_report.get("modules", []):
                extra = f", skipped_opening={mod['skipped_opening']}" if mod["skipped_opening"] else ""
                if mod.get("warning"):
                    print(f"       * {mod['module']}: skipped (unavailable/unauthorized)")
                elif execute:
                    print(f"       * {mod['module']}: found={mod['found']}, deleted={mod['deleted']}, failed={mod['failed']}{extra}")
                else:
                    print(f"       * {mod['module']}: found={mod['found']}{extra}")
            for w in zoho_report.get("warnings", []):
                print(f"   - Warning: {w}")

    if not execute:
        print("\nDry-run complete. Re-run with `--execute` to apply these changes.\n")
    else:
        print("\nLive reset complete.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset financial/HR/Zoho data while keeping customers, vendors, salaried staff & orders.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Inspect counts and mappings without modifying data (default).")
    mode_group.add_argument("--execute", action="store_true", help="Perform live Supabase & Zoho Books resets.")
    parser.add_argument("--skip-zoho", action="store_true", help="Skip Zoho Books inspection/wipe.")
    parser.add_argument("--skip-supabase", action="store_true", help="Skip Supabase inspection/reset.")
    parser.add_argument("--zero-zoho-opening", action="store_true", help="Also clear Zoho organization & contact opening balances so all accounts hit 0.00.")
    args = parser.parse_args(argv)

    execute = bool(args.execute)
    sb = None if args.skip_supabase else get_supabase_admin()
    sb_report = None if args.skip_supabase else reset_supabase_data(sb, execute=execute)
    zoho_report = None
    if not args.skip_zoho:
        load_cli_zoho_token_cache()
        try:
            zoho_report = asyncio.run(
                reset_zoho_transactions(execute=execute, zero_opening=bool(args.zero_zoho_opening))
            )
            if execute and zoho_report and sb is not None and zoho_report.get("enabled"):
                zoho_report["pulse_sync"] = asyncio.run(zoho.sync_owner_pulse_snapshot(sb))
        finally:
            save_cli_zoho_token_cache()
    _print_report(sb_report, zoho_report, execute=execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
