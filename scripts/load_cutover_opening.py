"""One-time pre-cutover opening data loader for Workshop Munim OS.

Loads raw opening figures cleanly WITHOUT touching any of the 6 active Bank/Cash balances:
  1. Active Customer Orders (`orders[]`): sets `quoted_total`, `paid_cache_inr`, and
     auto-derived `payment_stage` in Supabase `projects`.
  2. Supplier / Contractor Opening Udhaar (`vendor_udhaar[]`): posts idempotent `Open` AP
     Bills in Zoho Books against `Pre-Cutover Offset` so future `vendor_payment` entries
     settle them FIFO.
  3. Salaried Staff Base Salaries & Current-Month Advances (`salaried_staff[]`): updates
     `people.monthly_salary_inr`, upserts `worker_payroll_entries` (`entry_type='advance'`),
     and posts idempotent Zoho Expenses against `Pre-Cutover Offset` (with automatic
     `Pre-Cutover Clearing` fallback) using an em-dash description so Munim Passbook
     search surfaces them by worker name.

Defaults safely to `--dry-run`. Pass `--execute` to apply changes.

Usage:
    .\\backend\\.venv\\Scripts\\python.exe scripts\\load_cutover_opening.py --input cutover.json --dry-run
    .\\backend\\.venv\\Scripts\\python.exe scripts\\load_cutover_opening.py --input cutover.json --execute
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zoho_cli_cache import load_cli_zoho_token_cache, save_cli_zoho_token_cache  # noqa: E402
from app.db import get_supabase_admin  # noqa: E402
from app.services import zoho  # noqa: E402
from app.services.goal_primitives import IST, PAYMENT_TRACK_STAGES  # noqa: E402
from app.services.zoho.inventory_ledger import _clean_zoho_text  # noqa: E402


def _make_cutover_ref(prefix: str, key: str, max_len: int = 48) -> str:
    """Build a deterministic, collision-safe Zoho reference_number <= max_len chars."""
    slug = re.sub(r"[^a-z0-9]+", "_", (key or "").strip().lower()).strip("_") or "item"
    digest = hashlib.sha1((key or "").strip().lower().encode("utf-8")).hexdigest()[:6]
    room = max(8, max_len - len(prefix) - len(digest) - 2)
    return f"{prefix}_{slug[:room]}_{digest}"[:max_len]


def _derive_payment_stage(
    quoted_total: float,
    paid_inr: float,
    explicit_stage: str | None = None,
) -> str:
    if explicit_stage and explicit_stage in PAYMENT_TRACK_STAGES:
        return explicit_stage
    if paid_inr <= 0:
        return "advance_pending"
    if quoted_total > 0 and paid_inr >= quoted_total:
        return "payment_complete"
    if quoted_total > 0 and paid_inr >= quoted_total * 0.5:
        return "partial_paid"
    return "advance_in"


def _match_customer_project(
    projects: list[dict[str, Any]], entry: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    pcode = str(entry.get("project_code") or "").strip().lower()
    if pcode:
        hits = [
            p
            for p in projects
            if str(p.get("project_code") or "").strip().lower() == pcode
            or str(p.get("cf_project_code") or "").strip().lower() == pcode
        ]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, f"Multiple projects matched project_code '{entry.get('project_code')}'"

    cname = str(entry.get("customer_name") or "").strip().lower()
    if cname:
        hits = [
            p
            for p in projects
            if str(p.get("customer_name") or "").strip().lower() == cname
        ]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            codes = ", ".join(str(h.get("project_code") or h.get("id")) for h in hits)
            return None, f"Ambiguous customer '{entry.get('customer_name')}' (specify project_code from: {codes})"

    target_lbl = entry.get("project_code") or entry.get("customer_name") or "(missing identifier)"
    return None, f"No matching customer order found for '{target_lbl}'"


def _match_person(people_rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    non_contractors = [p for p in people_rows if p.get("person_type") != "contractor_mistri"]
    for p in non_contractors:
        if str(p.get("full_name") or "").strip().lower() == needle:
            return p
    for p in non_contractors:
        aliases = [str(a).strip().lower() for a in (p.get("aliases") or []) if str(a).strip()]
        if needle in aliases:
            return p
    return None


def _match_local_vendor(vendors: list[dict[str, Any]], vendor_name: str) -> dict[str, Any] | None:
    needle = (vendor_name or "").strip().lower()
    if not needle:
        return None
    for v in vendors:
        names = [str(v.get("canonical_name") or ""), *(v.get("aliases") or [])]
        if any(str(n).strip().lower() == needle for n in names if str(n).strip()):
            return v
    return None


async def _find_or_create_coa_account(
    client: httpx.AsyncClient,
    accounts: list[dict[str, Any]],
    *,
    preferred_names: tuple[str, ...],
    create_name: str,
    create_types: tuple[str, ...],
    description: str,
) -> dict[str, Any] | None:
    active = [a for a in (accounts or []) if a.get("is_active", True)]
    for target in preferred_names:
        hit = next(
            (a for a in active if (a.get("account_name") or "").strip().lower() == target.lower()),
            None,
        )
        if hit:
            return hit

    for acct_type in create_types:
        resp = await zoho._zoho_request(
            client,
            "POST",
            "/chartofaccounts",
            json_body={
                "account_name": create_name,
                "account_type": acct_type,
                "description": description,
            },
        )
        if resp.is_success:
            created = resp.json().get("chart_of_account") or resp.json().get("account") or {}
            if created.get("account_id"):
                zoho._COA_CACHE.clear()
                accounts.append(created)
                return created
    return None


def apply_order_openings(
    sb: Any, orders_data: list[dict[str, Any]], *, execute: bool
) -> dict[str, Any]:
    """Update active customer orders' Deal Value and Paid Till Date directly in Supabase."""
    if not orders_data:
        return {"processed": 0, "updated": 0, "errors": [], "items": []}

    projects = (
        sb.table("projects")
        .select("id,project_code,cf_project_code,customer_name,project_type,quoted_total,paid_cache_inr,payment_stage")
        .eq("project_type", "customer")
        .execute()
        .data
        or []
    )

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    updated = 0

    for entry in orders_data:
        prj, err = _match_customer_project(projects, entry)
        if err or not prj:
            errors.append(err or "Order match failed")
            continue

        quoted = (
            round(float(entry["quoted_total"]), 2)
            if entry.get("quoted_total") is not None
            else round(float(prj.get("quoted_total") or 0), 2)
        )
        paid = round(
            float(
                entry["paid_till_date_inr"]
                if entry.get("paid_till_date_inr") is not None
                else (entry.get("paid_cache_inr") or 0)
            ),
            2,
        )
        pstage = _derive_payment_stage(quoted, paid, entry.get("payment_stage"))
        patch = {
            "quoted_total": quoted,
            "paid_cache_inr": paid,
            "payment_stage": pstage,
        }
        if execute:
            sb.table("projects").update(patch).eq("id", prj["id"]).execute()
            updated += 1
        items.append(
            {
                "project_code": prj.get("project_code"),
                "customer_name": prj.get("customer_name"),
                "quoted_total": quoted,
                "paid_till_date_inr": paid,
                "payment_stage": pstage,
            }
        )

    return {"processed": len(items), "updated": updated, "errors": errors, "items": items}


async def apply_vendor_udhaar_openings(
    sb: Any,
    udhaar_data: list[dict[str, Any]],
    *,
    cutover_date: str,
    execute: bool,
    skip_zoho: bool = False,
) -> dict[str, Any]:
    """Post idempotent Open AP Bills against Pre-Cutover Offset without touching Bank/Cash."""
    if not udhaar_data:
        return {"processed": 0, "posted": 0, "deduped": 0, "errors": [], "items": []}

    vendors = (
        sb.table("vendors")
        .select("id,canonical_name,aliases,zoho_contact_id,is_active")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    settings = zoho.get_settings()
    zoho_live = bool(settings.zoho_enabled and not skip_zoho)

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    posted = 0
    deduped = 0

    if not execute or not zoho_live:
        for entry in udhaar_data:
            vname = str(entry.get("vendor_name") or "").strip()
            amt = round(float(entry.get("amount_inr") or 0), 2)
            if not vname or amt <= 0:
                continue
            matched = _match_local_vendor(vendors, vname)
            ref_no = _make_cutover_ref("cutover_ap", f"{vname}_{entry.get('bill_no') or ''}")
            items.append(
                {
                    "vendor_name": matched["canonical_name"] if matched else vname,
                    "matched_local": bool(matched),
                    "amount_inr": amt,
                    "reference_number": ref_no,
                    "status": "dry_run" if not execute else "zoho_skipped_or_stubbed",
                }
            )
        return {"processed": len(items), "posted": 0, "deduped": 0, "errors": errors, "items": items}

    async with httpx.AsyncClient(timeout=20.0) as client:
        accounts = await zoho._get_chart_of_accounts(client)
        offset_acc = await _find_or_create_coa_account(
            client,
            accounts,
            preferred_names=("pre-cutover offset", "pre-cutover clearing", "opening balance offset", "owner's equity"),
            create_name="Pre-Cutover Offset",
            create_types=("equity", "other_current_liability"),
            description="Pre-cutover opening AP/payroll offset (never touches active bank/cash)",
        )
        if not offset_acc:
            return {
                "processed": 0,
                "posted": 0,
                "deduped": 0,
                "errors": ["Could not find or create Pre-Cutover Offset account in Zoho Chart of Accounts."],
                "items": [],
            }

        for entry in udhaar_data:
            vname = str(entry.get("vendor_name") or "").strip()
            amt = round(float(entry.get("amount_inr") or 0), 2)
            if not vname or amt <= 0:
                continue
            bill_no = str(entry.get("bill_no") or "").strip()
            notes = str(entry.get("notes") or "").strip()
            ref_no = _make_cutover_ref("cutover_ap", f"{vname}_{bill_no}")

            existing_id = await zoho._find_existing_by_reference(
                client, "/bills", "bills", "bill_id", ref_no
            )
            if existing_id:
                deduped += 1
                items.append(
                    {
                        "vendor_name": vname,
                        "amount_inr": amt,
                        "reference_number": ref_no,
                        "zoho_bill_id": existing_id,
                        "status": "deduped",
                    }
                )
                continue

            payee = await zoho.resolve_payee_zoho_vendor(sb, vname, allow_adhoc_create=True)
            vid = payee.get("contact_id")
            if not vid:
                errors.append(f"Vendor '{vname}': {payee.get('error') or 'could not resolve Zoho contact'}")
                continue

            canon_name = payee.get("canonical_name") or vname
            slug_bill = re.sub(r"[^A-Z0-9]+", "", canon_name.upper())[:12] or "VENDOR"
            bill_payload = {
                "vendor_id": vid,
                "status": "open",
                "bill_number": _clean_zoho_text(bill_no or f"OPEN-{slug_bill}", 50),
                "date": cutover_date,
                "reference_number": ref_no,
                "is_inclusive_tax": False,
                "notes": _clean_zoho_text(f"[Pre-Cutover Opening Udhaar] {notes}".strip(), 240),
                "line_items": [
                    {
                        "account_id": offset_acc["account_id"],
                        "name": _clean_zoho_text(f"Pre-Cutover Opening Udhaar ({canon_name})", 95),
                        "quantity": 1,
                        "rate": amt,
                    }
                ],
            }
            resp = await zoho._zoho_request(client, "POST", "/bills", json_body=bill_payload)
            if not resp.is_success:
                errors.append(f"Vendor '{canon_name}' HTTP {resp.status_code}: {resp.text[:160]}")
                continue

            bill_obj = resp.json().get("bill") or {}
            bid = str(bill_obj.get("bill_id") or "")
            if bid and str(bill_obj.get("status") or "").lower() == "draft":
                await zoho._zoho_request(client, "POST", f"/bills/{bid}/status/open", json_body={})
            posted += 1
            items.append(
                {
                    "vendor_name": canon_name,
                    "amount_inr": amt,
                    "reference_number": ref_no,
                    "zoho_bill_id": bid,
                    "status": "open_bill_posted",
                }
            )

    return {"processed": len(items), "posted": posted, "deduped": deduped, "errors": errors, "items": items}


async def apply_salaried_staff_openings(
    sb: Any,
    staff_data: list[dict[str, Any]],
    *,
    cutover_date: str,
    salary_month: str,
    execute: bool,
    skip_zoho: bool = False,
) -> dict[str, Any]:
    """Update monthly_salary_inr and record pre-cutover month advances against Pre-Cutover Offset."""
    if not staff_data:
        return {"processed": 0, "salaries_updated": 0, "advances_saved": 0, "errors": [], "items": []}

    people_rows = (
        sb.table("people")
        .select("id,full_name,person_type,aliases,monthly_salary_inr,is_active")
        .execute()
        .data
        or []
    )
    settings = zoho.get_settings()
    zoho_live = bool(settings.zoho_enabled and not skip_zoho)

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    salaries_updated = 0
    advances_saved = 0

    client_ctx = httpx.AsyncClient(timeout=20.0) if (execute and zoho_live) else None
    client: httpx.AsyncClient | None = None
    accounts: list[dict[str, Any]] = []
    offset_acc: dict[str, Any] | None = None
    clearing_acc: dict[str, Any] | None = None
    salary_acc: dict[str, Any] | None = None

    try:
        if client_ctx is not None:
            client = await client_ctx.__aenter__()
            accounts = await zoho._get_chart_of_accounts(client)
            salary_acc = zoho._pick_zoho_expense_account(accounts, "salary_wages")
            offset_acc = await _find_or_create_coa_account(
                client,
                accounts,
                preferred_names=("pre-cutover offset", "opening balance offset", "owner's equity"),
                create_name="Pre-Cutover Offset",
                create_types=("equity", "other_current_liability"),
                description="Pre-cutover opening AP/payroll offset (never touches active bank/cash)",
            )

        for entry in staff_data:
            raw_name = str(entry.get("full_name") or "").strip()
            person = _match_person(people_rows, raw_name)
            if not person:
                errors.append(f"Salaried person '{raw_name}' not found in `people`")
                continue

            full_name = str(person["full_name"])
            salary_val = (
                round(float(entry["monthly_salary_inr"]), 2)
                if entry.get("monthly_salary_inr") is not None
                else None
            )
            adv_val = round(float(entry.get("advance_paid_inr") or 0), 2)
            notes = str(entry.get("notes") or "").strip()
            ref_no = _make_cutover_ref("cutover_adv", f"{salary_month}_{full_name}")

            if execute and salary_val is not None:
                sb.table("people").update({"monthly_salary_inr": salary_val}).eq("id", person["id"]).execute()
                salaries_updated += 1

            zoho_eid: str | None = None
            adv_status = "none"
            if adv_val > 0:
                adv_desc = _clean_zoho_text(
                    f"[Pre-Cutover] {full_name} — {salary_month} Salary Advance (Pre-Cutover)",
                    240,
                )
                if execute:
                    if client is not None and salary_acc is not None:
                        existing_eid = await zoho._find_existing_by_reference(
                            client, "/expenses", "expenses", "expense_id", ref_no
                        )
                        if existing_eid:
                            zoho_eid = existing_eid
                            adv_status = "deduped"
                        else:
                            paid_through = offset_acc
                            if not paid_through:
                                if clearing_acc is None:
                                    clearing_acc = await _find_or_create_coa_account(
                                        client,
                                        accounts,
                                        preferred_names=("pre-cutover clearing",),
                                        create_name="Pre-Cutover Clearing",
                                        create_types=("other_current_liability",),
                                        description="Pre-cutover paid-through clearing offset for salary advances",
                                    )
                                paid_through = clearing_acc

                            if not paid_through:
                                errors.append(f"No Zoho Pre-Cutover Offset/Clearing account for '{full_name}'")
                            else:
                                exp_payload = {
                                    "account_id": salary_acc["account_id"],
                                    "paid_through_account_id": paid_through["account_id"],
                                    "amount": adv_val,
                                    "date": cutover_date,
                                    "reference_number": ref_no,
                                    "description": adv_desc,
                                    "is_inclusive_tax": False,
                                }
                                resp = await zoho._zoho_request(client, "POST", "/expenses", json_body=exp_payload)
                                if not resp.is_success and resp.status_code == 400:
                                    if clearing_acc is None:
                                        clearing_acc = await _find_or_create_coa_account(
                                            client,
                                            accounts,
                                            preferred_names=("pre-cutover clearing",),
                                            create_name="Pre-Cutover Clearing",
                                            create_types=("other_current_liability",),
                                            description="Pre-cutover paid-through clearing offset for salary advances",
                                        )
                                    if clearing_acc and clearing_acc.get("account_id") != paid_through.get("account_id"):
                                        exp_payload["paid_through_account_id"] = clearing_acc["account_id"]
                                        resp = await zoho._zoho_request(client, "POST", "/expenses", json_body=exp_payload)

                                if resp.is_success:
                                    zoho_eid = str((resp.json().get("expense") or {}).get("expense_id") or "") or None
                                    adv_status = "posted"
                                else:
                                    errors.append(
                                        f"Salary advance Zoho expense for '{full_name}' HTTP {resp.status_code}: {resp.text[:160]}"
                                    )

                    existing_rows = (
                        sb.table("worker_payroll_entries")
                        .select("id,zoho_txn_id")
                        .eq("zoho_reference_number", ref_no)
                        .limit(1)
                        .execute()
                        .data
                        or []
                    )
                    payroll_payload = {
                        "person_id": person["id"],
                        "salary_month": salary_month,
                        "txn_date": cutover_date,
                        "entry_type": "advance",
                        "amount_inr": adv_val,
                        "payment_account_id": None,
                        "zoho_txn_id": zoho_eid or (existing_rows[0].get("zoho_txn_id") if existing_rows else None),
                        "zoho_reference_number": ref_no,
                        "notes": notes or f"{salary_month} salary advance (pre-cutover)",
                    }
                    if existing_rows:
                        sb.table("worker_payroll_entries").update(payroll_payload).eq(
                            "id", existing_rows[0]["id"]
                        ).execute()
                    else:
                        sb.table("worker_payroll_entries").insert(payroll_payload).execute()
                    advances_saved += 1
                    if adv_status == "none":
                        adv_status = "supabase_saved"
                else:
                    adv_status = "dry_run"

            items.append(
                {
                    "full_name": full_name,
                    "monthly_salary_inr": salary_val,
                    "advance_paid_inr": adv_val,
                    "reference_number": ref_no if adv_val > 0 else None,
                    "advance_status": adv_status,
                }
            )
    finally:
        if client_ctx is not None:
            await client_ctx.__aexit__(None, None, None)

    return {
        "processed": len(items),
        "salaries_updated": salaries_updated,
        "advances_saved": advances_saved,
        "errors": errors,
        "items": items,
    }


async def load_cutover_opening(
    sb: Any,
    payload: dict[str, Any],
    *,
    execute: bool,
    skip_zoho: bool = False,
) -> dict[str, Any]:
    """Run all three pre-cutover opening adjustments cleanly."""
    cutover_date = str(payload.get("cutover_date") or datetime.now(IST).date().isoformat()).strip()
    salary_month = str(payload.get("salary_month") or cutover_date[:7]).strip()

    orders_res = apply_order_openings(sb, list(payload.get("orders") or []), execute=execute)
    udhaar_res = await apply_vendor_udhaar_openings(
        sb,
        list(payload.get("vendor_udhaar") or []),
        cutover_date=cutover_date,
        execute=execute,
        skip_zoho=skip_zoho,
    )
    staff_res = await apply_salaried_staff_openings(
        sb,
        list(payload.get("salaried_staff") or []),
        cutover_date=cutover_date,
        salary_month=salary_month,
        execute=execute,
        skip_zoho=skip_zoho,
    )
    return {
        "executed": execute,
        "cutover_date": cutover_date,
        "salary_month": salary_month,
        "orders": orders_res,
        "vendor_udhaar": udhaar_res,
        "salaried_staff": staff_res,
    }


def _print_report(report: dict[str, Any]) -> None:
    mode = "LIVE EXECUTE" if report["executed"] else "DRY-RUN PREVIEW (no changes made)"
    print(f"\n=== Workshop Munim OS Opening Load [{mode}] ===")
    print(f"Cutover Date: {report['cutover_date']} | Salary Month: {report['salary_month']}\n")

    orders = report["orders"]
    print(f"1) ACTIVE CUSTOMER ORDERS: matched={orders['processed']}, updated={orders['updated']}")
    for row in orders["items"]:
        print(
            f"   * {row['project_code']} ({row['customer_name']}): "
            f"Deal={row['quoted_total']:,.2f} | Paid={row['paid_till_date_inr']:,.2f} -> {row['payment_stage']}"
        )
    for err in orders["errors"]:
        print(f"   ! ERROR: {err}")

    udhaar = report["vendor_udhaar"]
    print(f"\n2) SUPPLIER / CONTRACTOR UDHAAR: count={udhaar['processed']}, posted={udhaar['posted']}, deduped={udhaar['deduped']}")
    for row in udhaar["items"]:
        print(f"   * {row['vendor_name']}: {row['amount_inr']:,.2f} [{row['status']}] ({row['reference_number']})")
    for err in udhaar["errors"]:
        print(f"   ! ERROR: {err}")

    staff = report["salaried_staff"]
    print(
        f"\n3) SALARIED STAFF & ADVANCES: matched={staff['processed']}, "
        f"salaries_updated={staff['salaries_updated']}, advances_saved={staff['advances_saved']}"
    )
    for row in staff["items"]:
        sal_str = f"{row['monthly_salary_inr']:,.2f}" if row["monthly_salary_inr"] is not None else "unchanged"
        print(
            f"   * {row['full_name']}: Salary={sal_str} | "
            f"Advance={row['advance_paid_inr']:,.2f} [{row['advance_status']}]"
        )
    for err in staff["errors"]:
        print(f"   ! ERROR: {err}")

    if not report["executed"]:
        print("\nDry-run complete. Re-run with `--execute` to apply these opening figures.\n")
    else:
        print("\nOpening load complete.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load pre-cutover opening orders, vendor Udhaar, and staff salaries/advances.")
    parser.add_argument("--input", required=True, help="Path to JSON file with orders, vendor_udhaar, and salaried_staff.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Preview changes without mutating Supabase or Zoho (default).")
    mode_group.add_argument("--execute", action="store_true", help="Apply live updates to Supabase and Zoho Books.")
    parser.add_argument("--skip-zoho", action="store_true", help="Skip Zoho Books writes (Supabase only).")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not args.skip_zoho:
        load_cli_zoho_token_cache()
    try:
        report = asyncio.run(
            load_cutover_opening(
                get_supabase_admin(),
                data,
                execute=bool(args.execute),
                skip_zoho=bool(args.skip_zoho),
            )
        )
    finally:
        if not args.skip_zoho:
            save_cli_zoho_token_cache()
    _print_report(report)
    has_errors = any(
        report[section]["errors"] for section in ("orders", "vendor_udhaar", "salaried_staff")
    )
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
