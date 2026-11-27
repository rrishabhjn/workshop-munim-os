"""Zoho Books India Inventory Asset, Multi-Line Mixed Loads, Journals & FIFO AP Settlement."""

from __future__ import annotations

from datetime import datetime
import logging
import re
import sys
import time
from typing import Any, Callable

import httpx

from app.services.goal_primitives import IST, build_zoho_standard_order_tags, classify_external_error

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


def _clean_zoho_text(val: Any, max_len: int = 95) -> str:
    """Strip Zoho-forbidden `<`/`>`/control chars, normalize arrows, and cap length."""
    txt = str(val or "").replace("->", " for ").replace("<-", " from ")
    txt = re.sub(r"[\x00-\x1f\x7f<>]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len].strip()


def _extract_zoho_error(resp: httpx.Response, prefix: str) -> dict[str, Any]:
    if "json" in resp.headers.get("content-type", "").lower():
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("message"):
                return {"code": data.get("code"), "message": f"{prefix}: {data['message']}"}
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"message": f"{prefix} ({resp.status_code}): {resp.text[:160]}"}


async def _pick_or_create_inventory_asset_account(
    client: httpx.AsyncClient, accounts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve an active non-system Workshop Inventory Asset account or auto-create it once."""
    pkg = _pkg()
    active = [a for a in (accounts or []) if a.get("is_active", True)]
    # Exclude Zoho's built-in 'Inventory Asset' (account_type='stock', system-locked; triggers code 11016
    # on /journals, /expenses, and /bills) as well as system tax/advance accounts like 'Advance Tax'.
    eligible_assets = [
        a
        for a in active
        if a.get("account_type") in ("other_current_asset", "asset")
        and not a.get("is_system_account")
    ]
    for target in ("workshop inventory", "workshop stock"):
        hit = next(
            (a for a in eligible_assets if (a.get("account_name") or "").strip().lower() == target),
            None,
        )
        if hit:
            return hit

    for kw in ("inventory", "stock"):
        hit = next(
            (a for a in eligible_assets if kw in (a.get("account_name") or "").strip().lower()),
            None,
        )
        if hit:
            return hit

    try:
        resp = await pkg._zoho_request(
            client,
            "POST",
            "/chartofaccounts",
            json_body={
                "account_name": "Workshop Inventory",
                "account_type": "other_current_asset",
                "description": "Workshop raw materials inventory asset tracked by Munim OS",
            },
        )
        if resp.is_success:
            created = resp.json().get("chart_of_account") or resp.json().get("account") or {}
            if created.get("account_id"):
                pkg._COA_CACHE.clear()
                return created
    except Exception as exc:
        logger.warning("Auto-create Workshop Inventory CoA skipped: %s", exc)

    return pkg._pick_zoho_expense_account(accounts, "raw_materials")


def _pick_opening_or_variance_account(accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick an Equity / Opening / Stock Adjustment / Expense offset account for manual journals."""
    pkg = _pkg()
    active = [a for a in (accounts or []) if a.get("is_active", True) and a.get("account_type") != "stock"]
    for kw in (
        "opening balance offset",
        "owner's equity",
        "capital stock",
        "investments",
        "stock adjustment",
        "inventory adjustment",
        "opening balance",
        "retained earnings",
        "other expenses",
    ):
        hit = next(
            (a for a in active if kw in (a.get("account_name") or "").strip().lower()),
            None,
        )
        if hit:
            return hit
    eq = next((a for a in active if a.get("account_type") == "equity"), None)
    if eq:
        return eq
    return pkg._pick_zoho_expense_account(accounts, "raw_materials") or (active[0] if active else None)


async def push_inventory_journal_to_zoho(
    sb: Any,
    *,
    amount_inr: float,
    direction: str,
    reference_number: str,
    journal_date: str | None = None,
    notes: str = "",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Post a balanced Zoho Books Manual Journal for attribution or physical count variance."""
    pkg = _pkg()
    settings = pkg.get_settings()
    amt = round(float(amount_inr or 0), 2)
    if amt <= 0:
        return {"ok": True, "stubbed": True, "status_code": 200, "error_kind": "none", "zoho_type": "journal", "zoho_id": None}

    if not settings.zoho_enabled:
        return {
            "ok": True,
            "stubbed": True,
            "status_code": 200,
            "error_kind": "none",
            "zoho_type": "journal",
            "zoho_id": f"STUB-{reference_number}",
        }

    order_tags: dict[str, Any] = {}
    bridge: dict[str, Any] = {}
    if project_id:
        bridge = await pkg.ensure_project_zoho_bridge(sb, project_id)
        order_tags = build_zoho_standard_order_tags(
            project_code=bridge.get("cf_project_code"),
            zoho_tag_id=bridge.get("zoho_tag_id") or settings.zoho_project_tag_id,
            zoho_tag_option_id=bridge.get("zoho_tag_option_id"),
            custom_field_api_name=settings.zoho_project_custom_field,
        )

    line_tags = {"tags": order_tags["tags"]} if order_tags.get("tags") else {}
    proj_prefix = f"[{bridge['cf_project_code']}] " if bridge.get("cf_project_code") else ""
    clean_date = journal_date or datetime.now(IST).date().isoformat()

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            existing_id = await pkg._find_existing_by_reference(
                client, "/journals", "journals", "journal_id", reference_number
            )
            if existing_id:
                return {
                    "ok": True,
                    "status_code": 200,
                    "error_kind": "none",
                    "zoho_type": "journal",
                    "zoho_id": existing_id,
                    "deduped": True,
                }

            accounts = await pkg._get_chart_of_accounts(client)
            inv_asset = await _pick_or_create_inventory_asset_account(client, accounts)
            cogs_acc = pkg._pick_zoho_expense_account(accounts, "raw_materials")
            var_acc = _pick_opening_or_variance_account(accounts)
            if not inv_asset or not (cogs_acc or var_acc):
                return {
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {"message": "Missing Workshop Inventory or COGS account in Zoho Chart of Accounts."},
                }

            if direction in ("attribute_to_customer", "shrinkage_writeoff"):
                debit_acc = cogs_acc or var_acc
                credit_acc = inv_asset
                debit_tags = line_tags if direction == "attribute_to_customer" else {}
            else:
                debit_acc = inv_asset
                credit_acc = var_acc or cogs_acc
                debit_tags = {}

            desc = _clean_zoho_text(f"[Inventory] {proj_prefix}{notes}", 95) or "[Inventory]"
            payload: dict[str, Any] = {
                "journal_date": clean_date,
                "reference_number": reference_number,
                "notes": desc,
                "line_items": [
                    {
                        "account_id": debit_acc["account_id"],
                        "debit_or_credit": "debit",
                        "amount": amt,
                        "description": desc,
                        **debit_tags,
                    },
                    {
                        "account_id": credit_acc["account_id"],
                        "debit_or_credit": "credit",
                        "amount": amt,
                        "description": desc,
                    },
                ],
            }
            resp = await pkg._post_zoho_with_order_tag_fallback(
                client, "/journals", payload, has_order_tags=bool(debit_tags)
            )
            if resp.is_success:
                jid = (resp.json().get("journal") or {}).get("journal_id")
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "error_kind": "none",
                    "zoho_type": "journal",
                    "zoho_id": jid,
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_kind": classify_external_error(resp.status_code),
                "error": _extract_zoho_error(resp, "Zoho Journal error"),
            }
    except httpx.TimeoutException as exc:
        return {"ok": False, "status_code": None, "error_kind": "transient_5xx", "error": {"message": f"Zoho journal timeout: {exc}"}}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 503, "error_kind": "transient_5xx", "error": {"message": f"Zoho journal error: {exc}"}}


async def push_mixed_inventory_load_to_zoho(
    sb: Any,
    *,
    settlement_mode: str,
    lines: list[dict[str, Any]],
    reference_number: str,
    payment_account_id: str | None = None,
    vendor_name: str = "",
    vendor_zoho_id: str | None = None,
    vendor_phone: str | None = None,
    vendor_gstin: str | None = None,
    invoice_no: str | None = None,
    txn_date: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Push a multi-line Stock + Direct Customer Gate-In as Expense, Open AP Bill, or Manual Journal."""
    pkg = _pkg()
    settings = pkg.get_settings()
    ztype_map = {"paid_now": "expense", "vendor_udhaar": "bill_open", "manual": "journal"}
    ztype = ztype_map.get(settlement_mode, "journal")

    if not settings.zoho_enabled:
        return {
            "ok": True,
            "stubbed": True,
            "status_code": 200,
            "error_kind": "none",
            "zoho_type": ztype,
            "zoho_id": f"STUB-{reference_number}",
        }

    total_amt = round(sum(float(ln.get("total_inr") or 0) for ln in lines), 2)
    if total_amt <= 0:
        return {"ok": True, "stubbed": True, "status_code": 200, "error_kind": "none", "zoho_type": ztype, "zoho_id": None}

    clean_date = txn_date or datetime.now(IST).date().isoformat()
    bridges: dict[str, dict[str, Any]] = {}
    for ln in lines:
        pid = str(ln.get("project_id") or "").strip()
        if pid and pid not in bridges:
            bridges[pid] = await pkg.ensure_project_zoho_bridge(sb, pid)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if settlement_mode == "paid_now":
                existing = await pkg._find_existing_by_reference(client, "/expenses", "expenses", "expense_id", reference_number)
            elif settlement_mode == "vendor_udhaar":
                existing = await pkg._find_existing_by_reference(client, "/bills", "bills", "bill_id", reference_number)
            else:
                existing = await pkg._find_existing_by_reference(client, "/journals", "journals", "journal_id", reference_number)
            if existing:
                return {"ok": True, "status_code": 200, "error_kind": "none", "zoho_type": ztype, "zoho_id": existing, "deduped": True}

            if settlement_mode == "vendor_udhaar" and invoice_no:
                dup = await pkg.check_duplicate_bill(sb, vendor_name or "", invoice_no, total_amt)
                if dup:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {
                            "code": "DUPLICATE_BILL",
                            "message": f"Bill '{invoice_no}' for '{vendor_name}' (₹{total_amt:,.2f}) already exists in {dup['source']}.",
                        },
                    }

            accounts = await pkg._get_chart_of_accounts(client)
            inv_asset = await _pick_or_create_inventory_asset_account(client, accounts)
            cogs_acc = pkg._pick_zoho_expense_account(accounts, "raw_materials")
            var_acc = _pick_opening_or_variance_account(accounts)
            if not inv_asset or not (cogs_acc or var_acc):
                return {"ok": False, "status_code": 400, "error_kind": "validation_400", "error": {"message": "Missing Zoho Inventory/COGS Chart of Accounts entry."}}

            def _line_tags(pid: str) -> dict[str, Any]:
                br = bridges.get(pid) or {}
                ot = build_zoho_standard_order_tags(
                    project_code=br.get("cf_project_code"),
                    zoho_tag_id=br.get("zoho_tag_id") or settings.zoho_project_tag_id,
                    zoho_tag_option_id=br.get("zoho_tag_option_id"),
                    custom_field_api_name=settings.zoho_project_custom_field,
                )
                return {"tags": ot["tags"]} if ot.get("tags") else {}

            has_any_tags = any(bool(_line_tags(str(ln.get("project_id") or ""))) for ln in lines)

            if settlement_mode == "paid_now":
                acc_rows = sb.table("payment_accounts").select("*").eq("id", payment_account_id).limit(1).execute().data or []
                if not acc_rows:
                    return {"ok": False, "status_code": 400, "error_kind": "validation_400", "error": {"message": "Payment account is required for Paid Now load."}}
                from_zoho_acc = await pkg._resolve_zoho_paid_account_id(client, sb, acc_rows[0])
                if not from_zoho_acc:
                    return {"ok": False, "status_code": 400, "error_kind": "validation_400", "error": {"message": f"Account '{acc_rows[0]['display_name']}' is not mapped in Zoho Books."}}

                exp_lines = []
                for ln in lines:
                    pid = str(ln.get("project_id") or "").strip()
                    is_cust = ln.get("destination") == "order" and bool(pid)
                    lacc = (cogs_acc or inv_asset) if is_cust else inv_asset
                    exp_lines.append({
                        "account_id": lacc["account_id"],
                        "amount": round(float(ln["total_inr"]), 2),
                        "description": _clean_zoho_text(
                            f"{ln['material_label']} ({ln['quantity']:g} {ln['unit']})", 95
                        ),
                        **(_line_tags(pid) if is_cust else {}),
                    })
                vendor_id = vendor_zoho_id
                resolved_vendor_id: str | None = None
                if not vendor_id and vendor_name:
                    payee = await pkg.resolve_payee_zoho_vendor(
                        sb, vendor_name, allow_adhoc_create=True, phone=vendor_phone, gstin=vendor_gstin
                    )
                    vendor_id = payee.get("contact_id")
                    resolved_vendor_id = payee.get("vendor_id")
                exp_payload: dict[str, Any] = {
                    **({"account_id": exp_lines[0]["account_id"]} if len(exp_lines) == 1 else {}),
                    "paid_through_account_id": from_zoho_acc,
                    "amount": total_amt,
                    "date": clean_date,
                    "reference_number": reference_number,
                    "description": _clean_zoho_text(f"[Inventory Load] {vendor_name} {notes}", 240),
                    "is_inclusive_tax": False,
                    "line_items": exp_lines,
                    **({"vendor_id": vendor_id} if vendor_id else {}),
                }
                resp = await pkg._post_zoho_with_order_tag_fallback(client, "/expenses", exp_payload, has_order_tags=has_any_tags)
                if resp.is_success:
                    eid = (resp.json().get("expense") or {}).get("expense_id")
                    return {"ok": True, "status_code": resp.status_code, "error_kind": "none", "zoho_type": "expense", "zoho_id": eid, **({"vendor_id": resolved_vendor_id} if resolved_vendor_id else {})}
                return {"ok": False, "status_code": resp.status_code, "error_kind": classify_external_error(resp.status_code), "error": _extract_zoho_error(resp, "Zoho Expense error")}

            if settlement_mode == "vendor_udhaar":
                vid = vendor_zoho_id
                vendor_err: str | None = None
                resolved_vendor_id = None
                if not vid and vendor_name:
                    payee = await pkg.resolve_payee_zoho_vendor(
                        sb, vendor_name, allow_adhoc_create=True, phone=vendor_phone, gstin=vendor_gstin
                    )
                    vid = payee.get("contact_id")
                    vendor_err = payee.get("error")
                    resolved_vendor_id = payee.get("vendor_id")
                if not vid:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": vendor_err or f"Vendor '{vendor_name}' could not be resolved in Zoho Books."},
                    }

                bill_lines = []
                for ln in lines:
                    pid = str(ln.get("project_id") or "").strip()
                    is_cust = ln.get("destination") == "order" and bool(pid)
                    lacc = (cogs_acc or inv_asset) if is_cust else inv_asset
                    bill_lines.append({
                        "account_id": lacc["account_id"],
                        "name": _clean_zoho_text(ln["material_label"], 95) or "Workshop Material",
                        "quantity": float(ln["quantity"]),
                        "rate": round(float(ln["unit_rate_inr"]), 2),
                        **(_line_tags(pid) if is_cust else {}),
                    })
                bill_payload: dict[str, Any] = {
                    "vendor_id": vid,
                    "status": "open",
                    "bill_number": _clean_zoho_text(invoice_no or f"INV-{int(time.time())}", 50),
                    "date": clean_date,
                    "reference_number": reference_number,
                    "is_inclusive_tax": False,
                    "notes": _clean_zoho_text(f"[Inventory Gate-In AP] {notes}", 240),
                    "line_items": bill_lines,
                }
                b_resp = await pkg._post_zoho_with_order_tag_fallback(client, "/bills", bill_payload, has_order_tags=has_any_tags)
                if not b_resp.is_success:
                    return {"ok": False, "status_code": b_resp.status_code, "error_kind": classify_external_error(b_resp.status_code), "error": _extract_zoho_error(b_resp, "Zoho Open Bill error")}
                bill_obj = b_resp.json().get("bill") or {}
                bid = str(bill_obj.get("bill_id") or "")
                if bid and str(bill_obj.get("status") or "").lower() == "draft":
                    await pkg._zoho_request(client, "POST", f"/bills/{bid}/status/open", json_body={})
                return {"ok": True, "status_code": b_resp.status_code, "error_kind": "none", "zoho_type": "bill_open", "zoho_id": bid, **({"vendor_id": resolved_vendor_id} if resolved_vendor_id else {})}

            # manual / opening load -> balanced Journal
            j_lines = []
            for ln in lines:
                pid = str(ln.get("project_id") or "").strip()
                is_cust = ln.get("destination") == "order" and bool(pid)
                lacc = (cogs_acc or inv_asset) if is_cust else inv_asset
                j_lines.append({
                    "account_id": lacc["account_id"],
                    "debit_or_credit": "debit",
                    "amount": round(float(ln["total_inr"]), 2),
                    "description": _clean_zoho_text(
                        f"{ln['material_label']} ({ln['quantity']:g} {ln['unit']})", 95
                    ),
                    **(_line_tags(pid) if is_cust else {}),
                })
            manual_note = _clean_zoho_text(f"[Manual Inventory Load] {notes}", 95)
            j_lines.append({
                "account_id": (var_acc or cogs_acc)["account_id"],
                "debit_or_credit": "credit",
                "amount": total_amt,
                "description": manual_note,
            })
            j_payload = {
                "journal_date": clean_date,
                "reference_number": reference_number,
                "notes": manual_note,
                "line_items": j_lines,
            }
            j_resp = await pkg._post_zoho_with_order_tag_fallback(client, "/journals", j_payload, has_order_tags=has_any_tags)
            if j_resp.is_success:
                jid = (j_resp.json().get("journal") or {}).get("journal_id")
                return {"ok": True, "status_code": j_resp.status_code, "error_kind": "none", "zoho_type": "journal", "zoho_id": jid}
            return {"ok": False, "status_code": j_resp.status_code, "error_kind": classify_external_error(j_resp.status_code), "error": _extract_zoho_error(j_resp, "Zoho Journal error")}
    except httpx.TimeoutException as exc:
        return {"ok": False, "status_code": None, "error_kind": "transient_5xx", "error": {"message": f"Zoho inventory request timed out: {exc}"}}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 503, "error_kind": "transient_5xx", "error": {"message": f"Zoho inventory request failed: {exc}"}}


async def build_fifo_vendor_bill_allocations(
    client: httpx.AsyncClient, vendor_zoho_id: str, amount_inr: float
) -> list[dict[str, Any]]:
    """Allocate a vendor payment oldest-first (FIFO) across open Zoho Accounts Payable bills."""
    pkg = _pkg()
    remaining = round(float(amount_inr or 0), 2)
    if not vendor_zoho_id or remaining <= 0:
        return []
    try:
        resp = await pkg._zoho_request(
            client, "GET", "/bills", params={"vendor_id": vendor_zoho_id, "status": "open"}
        )
        if not resp.is_success:
            return []
        bills = list(resp.json().get("bills") or [])
    except Exception:
        return []

    open_bills = [
        b
        for b in bills
        if b.get("bill_id") and float(b.get("balance") if b.get("balance") is not None else b.get("total") or 0) > 0
    ]
    open_bills.sort(key=lambda b: (str(b.get("date") or ""), str(b.get("created_time") or ""), str(b.get("bill_id") or "")))

    allocations: list[dict[str, Any]] = []
    for b in open_bills:
        if remaining <= 0:
            break
        bal = round(float(b.get("balance") if b.get("balance") is not None else b.get("total") or 0), 2)
        apply_amt = round(min(remaining, bal), 2)
        if apply_amt > 0:
            allocations.append({"bill_id": str(b["bill_id"]), "amount_applied": apply_amt})
            remaining = round(remaining - apply_amt, 2)
    return allocations


def extract_project_matched_doc_amount(
    doc: dict[str, Any],
    cf_name: str,
    matches_fn: Callable[[set[str], set[str], str], bool],
    *,
    is_journal: bool = False,
) -> float:
    """Sum line-level Reporting Tag amounts (falling back to header total for untagged lines)."""
    pkg = _pkg()
    line_items = doc.get("line_items") or []
    any_line_tagged = False
    matched_sum = 0.0
    for li in line_items:
        if not isinstance(li, dict):
            continue
        li_opts, li_codes, li_zp = pkg._extract_zoho_doc_project_keys(li, cf_name)
        if li_opts or li_codes or li_zp:
            any_line_tagged = True
            if matches_fn(li_opts, li_codes, li_zp):
                raw_amt = float(li.get("total") if li.get("total") is not None else li.get("amount") or (float(li.get("rate") or 0) * float(li.get("quantity") or 1)))
                if is_journal and str(li.get("debit_or_credit") or "").lower() == "credit":
                    matched_sum -= raw_amt
                else:
                    matched_sum += raw_amt
    if any_line_tagged:
        return round(matched_sum, 2)

    doc_opts, doc_codes, doc_zp = pkg._extract_zoho_doc_project_keys(doc, cf_name)
    if matches_fn(doc_opts, doc_codes, doc_zp):
        return round(float(doc.get("total") or 0), 2)
    return 0.0
