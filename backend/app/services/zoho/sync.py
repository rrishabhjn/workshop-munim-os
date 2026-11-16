"""Nightly & manual Zoho Books India project P&L cache sync and Owner Pulse snapshot sync."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any

import httpx

from app.services.zoho.contacts import NON_PAYEE_WORDS

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


async def sync_project_financial_caches(sb: Any) -> dict[str, Any]:
    """Nightly 02:00 IST & manual refresh of zoho_invoiced_cache / zoho_bills_cache.

    Aggregates non-void Zoho invoices & bills per active project using Zoho Standard Plan
    Reporting Tags (zoho_tag_option_id) and cf_project_code, with legacy zoho_project_id fallback.
    """
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {"synced": 0, "stubbed": True, "reason": "Zoho credentials not configured"}

    projects = (
        sb.table("projects")
        .select("id,project_code,cf_project_code,zoho_project_id,zoho_tag_id,zoho_tag_option_id")
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    if not projects:
        return {"synced": 0, "stubbed": False}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            inv_ok, invoices = await pkg._list_all_zoho_docs(client, "/invoices", "invoices")
            bill_ok, bills = await pkg._list_all_zoho_docs(client, "/bills", "bills")
    except httpx.HTTPError as exc:
        logger.warning("sync_project_financial_caches failed: %s", exc)
        return {"synced": 0, "stubbed": False, "error": str(exc)}

    if not inv_ok and not bill_ok:
        return {"synced": 0, "stubbed": False, "error": "Failed to fetch Zoho invoices and bills"}

    cf_name = settings.zoho_project_custom_field
    inv_docs = [
        (doc, *pkg._extract_zoho_doc_project_keys(doc, cf_name))
        for doc in invoices
        if doc.get("status") != "void"
    ]
    bill_docs = [
        (doc, *pkg._extract_zoho_doc_project_keys(doc, cf_name))
        for doc in bills
        if doc.get("status") != "void"
    ]

    synced = 0
    for prj in projects:
        opt_id = str(prj.get("zoho_tag_option_id") or "").strip()
        pcode = str(prj.get("cf_project_code") or prj.get("project_code") or "").strip().lower()
        zp_id = str(prj.get("zoho_project_id") or "").strip()
        if not (opt_id or pcode or zp_id):
            continue

        def _matches(tag_opts: set[str], codes: set[str], doc_zp: str) -> bool:
            if opt_id and opt_id in tag_opts:
                return True
            if pcode and pcode in codes:
                return True
            if zp_id and doc_zp and zp_id == doc_zp:
                return True
            return False

        patch: dict[str, Any] = {"zoho_synced_at": "now()"}
        if inv_ok:
            inv_total = sum(
                float(doc.get("total") or 0)
                for doc, t_opts, codes, doc_zp in inv_docs
                if _matches(t_opts, codes, doc_zp)
            )
            patch["zoho_invoiced_cache"] = round(inv_total, 2)
        if bill_ok:
            bill_total = sum(
                float(doc.get("total") or 0)
                for doc, t_opts, codes, doc_zp in bill_docs
                if _matches(t_opts, codes, doc_zp)
            )
            patch["zoho_bills_cache"] = round(bill_total, 2)

        sb.table("projects").update(patch).eq("id", prj["id"]).execute()
        synced += 1

    return {"synced": synced, "stubbed": False}


async def sync_owner_pulse_snapshot(sb: Any) -> dict[str, Any]:
    """Refresh Zoho Bank/Cash/Float balances into payment_accounts.cached_balance_inr."""
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {"ok": True, "stubbed": True, "accounts_synced": 0}

    synced = 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await pkg._zoho_request(client, "GET", "/bankaccounts")
            if not resp.is_success:
                return {"ok": False, "error": f"Zoho /bankaccounts HTTP {resp.status_code}"}
            zoho_accs = resp.json().get("bankaccounts") or []
            local_accs = sb.table("payment_accounts").select("*").eq("is_active", True).execute().data or []
            local_accs.sort(key=lambda a: int(a.get("sort_order") or 0))
            zoho_by_id = {
                str(z.get("account_id")): z
                for z in zoho_accs
                if z.get("account_id") and z.get("is_active") is not False
            }
            claimed_zoho_ids = {
                str(la["zoho_account_id"])
                for la in local_accs
                if la.get("zoho_account_id") and str(la["zoho_account_id"]) in zoho_by_id
            }
            for la in local_accs:
                match = None
                if la.get("zoho_account_id"):
                    match = zoho_by_id.get(str(la["zoho_account_id"]))
                if not match:
                    match = pkg._pick_matching_zoho_bank_account(la, zoho_accs, claimed_zoho_ids)
                if match:
                    zid = str(match["account_id"])
                    claimed_zoho_ids.add(zid)
                    sb.table("payment_accounts").update(
                        {
                            "zoho_account_id": zid,
                            "cached_balance_inr": round(float(match.get("balance") or 0), 2),
                            "balance_synced_at": "now()",
                        }
                    ).eq("id", la["id"]).execute()
                    synced += 1
    except httpx.HTTPError as exc:
        logger.warning("sync_owner_pulse_snapshot failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "stubbed": False, "accounts_synced": synced}


def _extract_party_from_desc(desc: str) -> str:
    clean = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", (desc or "").strip()).strip()
    if "—" in clean:
        cand = clean.split("—", 1)[0].strip(" :-")
        if cand and len(cand) <= 45 and cand.lower() not in NON_PAYEE_WORDS:
            return cand
    return ""


async def list_recent_ledger_transactions(
    sb: Any,
    *,
    limit: int = 50,
    account_query: str = "",
    party_query: str = "",
    entry_type: str = "",
    date_from: str = "",
    date_to: str = "",
) -> list[dict[str, Any]]:
    """Fetch read-only chronological ledger entries from live Zoho Books (or local completed goals when Zoho is stubbed)."""
    pkg = _pkg()
    settings = pkg.get_settings()
    clamped_limit = max(1, min(int(limit or 50), 200))

    et_map = {
        "all": "",
        "receipt": "customer_receipt",
        "receipts": "customer_receipt",
        "expenses": "expense",
        "payments": "vendor_payment",
        "transfer": "float_transfer",
        "transfers": "float_transfer",
        "bills": "bill",
    }
    et_clean = et_map.get(entry_type.strip().lower(), entry_type.strip().lower())

    aq = account_query.strip().lower()
    account_needles: set[str] = {aq} if aq else set()
    account_zoho_ids: set[str] = set()
    local_acc_names: dict[str, str] = {}

    if sb is not None:
        local_accs = (
            sb.table("payment_accounts")
            .select("id,code,display_name,aliases,zoho_account_id,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        for la in local_accs:
            if la.get("code"):
                local_acc_names[str(la["code"]).lower()] = str(la.get("display_name") or la["code"])
            if la.get("id"):
                local_acc_names[str(la["id"])] = str(la.get("display_name") or "")
            if aq:
                tokens = [
                    str(la.get("code") or ""),
                    str(la.get("display_name") or ""),
                    str(la.get("zoho_account_id") or ""),
                    *(str(a) for a in (la.get("aliases") or [])),
                ]
                if any(aq == t.strip().lower() or aq in t.strip().lower() for t in tokens if t.strip()):
                    if la.get("display_name"):
                        account_needles.add(str(la["display_name"]).strip().lower())
                    if la.get("code"):
                        account_needles.add(str(la["code"]).strip().lower().replace("_", " "))
                    if la.get("zoho_account_id"):
                        account_zoho_ids.add(str(la["zoho_account_id"]))

    pq = party_query.strip().lower()
    party_needles: set[str] = {pq} if pq else set()
    if sb is not None and pq:
        for tbl, cols in (
            ("people", ("full_name", "aliases", "payout_aliases")),
            ("vendors", ("canonical_name", "aliases")),
            ("customers", ("full_name", "aliases")),
        ):
            for rec in sb.table(tbl).select(",".join(cols)).execute().data or []:
                vals: list[str] = []
                for c in cols:
                    v = rec.get(c)
                    if isinstance(v, list):
                        vals.extend(str(x).strip() for x in v if str(x).strip())
                    elif v and str(v).strip():
                        vals.append(str(v).strip())
                if any(pq in v.lower() for v in vals):
                    party_needles.update(v.lower() for v in vals)

    raw_rows: list[dict[str, Any]] = []
    if settings.zoho_enabled:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                reqs = [
                    pkg._zoho_request(client, "GET", "/expenses", params={"per_page": 100}),
                    pkg._zoho_request(client, "GET", "/customerpayments", params={"per_page": 100}),
                    pkg._zoho_request(client, "GET", "/vendorpayments", params={"per_page": 100}),
                    pkg._zoho_request(client, "GET", "/banktransactions", params={"per_page": 100}),
                ]
                if et_clean in ("", "bill"):
                    reqs.append(pkg._zoho_request(client, "GET", "/bills", params={"per_page": 60}))
                resps = await asyncio.gather(*reqs, return_exceptions=True)

            def _items(idx: int, key: str) -> list[dict[str, Any]]:
                if idx >= len(resps) or isinstance(resps[idx], Exception):
                    return []
                r = resps[idx]
                return (r.json().get(key) or []) if getattr(r, "is_success", False) else []

            seen_ids: set[str] = set()
            seen_transfers: set[tuple[str, float]] = set()

            for x in _items(0, "expenses"):
                eid = str(x.get("expense_id") or "")
                if eid:
                    seen_ids.add(eid)
                raw_rows.append(
                    {
                        "id": eid,
                        "date": str(x.get("date") or ""),
                        "created_time": str(x.get("created_time") or ""),
                        "entry_type": "expense",
                        "direction": "out",
                        "amount_inr": round(float(x.get("total") or x.get("bcy_total") or 0), 2),
                        "account_name": str(x.get("paid_through_account_name") or ""),
                        "_account_id": str(x.get("paid_through_account_id") or ""),
                        "category_or_offset": str(x.get("account_name") or "Expense"),
                        "party_name": str(x.get("vendor_name") or "").strip()
                        or _extract_party_from_desc(str(x.get("description") or "")),
                        "customer_name": str(x.get("customer_name") or "").strip(),
                        "description": str(x.get("description") or ""),
                        "reference_number": str(x.get("reference_number") or ""),
                        "status": str(x.get("status") or ""),
                    }
                )

            for x in _items(1, "customerpayments"):
                pid = str(x.get("payment_id") or "")
                if pid:
                    seen_ids.add(pid)
                cname = str(x.get("customer_name") or "").strip()
                raw_rows.append(
                    {
                        "id": pid,
                        "date": str(x.get("date") or ""),
                        "created_time": str(x.get("created_time") or ""),
                        "entry_type": "customer_receipt",
                        "direction": "in",
                        "amount_inr": round(float(x.get("amount") or x.get("bcy_amount") or 0), 2),
                        "account_name": str(x.get("account_name") or x.get("deposit_to_account_name") or ""),
                        "_account_id": str(x.get("account_id") or x.get("deposit_to_account_id") or ""),
                        "category_or_offset": "Customer Receipt",
                        "party_name": cname,
                        "customer_name": cname,
                        "description": str(x.get("description") or ""),
                        "reference_number": str(x.get("reference_number") or x.get("payment_number") or ""),
                        "status": str(x.get("status") or "paid"),
                    }
                )

            for x in _items(2, "vendorpayments"):
                vpid = str(x.get("payment_id") or "")
                if vpid:
                    seen_ids.add(vpid)
                raw_rows.append(
                    {
                        "id": vpid,
                        "date": str(x.get("date") or ""),
                        "created_time": str(x.get("created_time") or ""),
                        "entry_type": "vendor_payment",
                        "direction": "out",
                        "amount_inr": round(float(x.get("amount") or x.get("bcy_amount") or 0), 2),
                        "account_name": str(x.get("paid_through_account_name") or ""),
                        "_account_id": str(x.get("paid_through_account_id") or ""),
                        "category_or_offset": "Vendor Payment / Advance",
                        "party_name": str(x.get("vendor_name") or "").strip(),
                        "customer_name": "",
                        "description": str(x.get("description") or ""),
                        "reference_number": str(x.get("reference_number") or x.get("payment_number") or ""),
                        "status": str(x.get("status") or "paid"),
                    }
                )

            for x in _items(3, "banktransactions"):
                tid = str(x.get("transaction_id") or "")
                if not tid or tid in seen_ids:
                    continue
                ttype = str(x.get("transaction_type") or "").lower()
                amt = round(float(x.get("amount") or 0), 2)
                ref = str(x.get("reference_number") or "")
                dt = str(x.get("date") or "")
                offset_name = str(x.get("offset_account_name") or "").strip()
                if "transfer" in ttype:
                    xfer_key = (ref or dt, amt)
                    if xfer_key in seen_transfers:
                        continue
                    seen_transfers.add(xfer_key)
                    mapped_type, direction = "float_transfer", "transfer"
                    cat = f"Transfer -> {offset_name}".strip(" ->") or "Float Transfer"
                else:
                    is_out = str(x.get("debit_or_credit") or "").lower() == "credit"
                    mapped_type = "expense" if is_out else "customer_receipt"
                    direction = "out" if is_out else "in"
                    cat = str(x.get("transaction_type_formatted") or offset_name or "Bank Entry")
                raw_rows.append(
                    {
                        "id": tid,
                        "date": dt,
                        "created_time": str(x.get("created_time") or ""),
                        "entry_type": mapped_type,
                        "direction": direction,
                        "amount_inr": amt,
                        "account_name": str(x.get("account_name") or ""),
                        "_account_id": str(x.get("account_id") or ""),
                        "category_or_offset": cat,
                        "party_name": str(x.get("payee") or "").strip()
                        or _extract_party_from_desc(str(x.get("description") or "")),
                        "customer_name": "",
                        "description": str(x.get("description") or ""),
                        "reference_number": ref,
                        "status": str(x.get("status") or ""),
                    }
                )

            for x in _items(4, "bills"):
                bid = str(x.get("bill_id") or "")
                raw_rows.append(
                    {
                        "id": bid,
                        "date": str(x.get("date") or ""),
                        "created_time": str(x.get("created_time") or ""),
                        "entry_type": "bill",
                        "direction": "bill",
                        "amount_inr": round(float(x.get("total") or x.get("bcy_total") or 0), 2),
                        "account_name": "",
                        "_account_id": "",
                        "category_or_offset": f"Supplier Bill ({x.get('status') or 'draft'})",
                        "party_name": str(x.get("vendor_name") or "").strip(),
                        "customer_name": "",
                        "description": f"Bill #{x.get('bill_number') or ''} {x.get('notes') or ''}".strip(),
                        "reference_number": str(x.get("reference_number") or x.get("bill_number") or ""),
                        "status": str(x.get("status") or "draft"),
                    }
                )
        except httpx.HTTPError as exc:
            logger.warning("list_recent_ledger_transactions Zoho fetch failed: %s", exc)
            return []
    elif sb is not None:
        goals = (
            sb.table("agent_goals")
            .select("id,goal_type,summary_title,collected_slots,steps,created_at,resolved_at")
            .eq("status", "completed")
            .in_("goal_type", ["financial_txn", "float_transfer", "supplier_bill"])
            .order("created_at", desc=True)
            .limit(150)
            .execute()
            .data
            or []
        )
        for g in goals:
            slots = g.get("collected_slots") or {}
            steps = g.get("steps") or []
            op = str(slots.get("operation_kind") or (steps[0].get("operation") if steps else "") or "expense")
            if op not in ("customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill"):
                continue
            mapped_type = "bill" if op == "supplier_draft_bill" else op
            direction = (
                "in"
                if op == "customer_receipt"
                else ("transfer" if op == "float_transfer" else ("bill" if op == "supplier_draft_bill" else "out"))
            )
            acc_label = (
                local_acc_names.get(str(slots.get("payment_account_id") or ""))
                or local_acc_names.get(str(slots.get("payment_account_code") or "").lower())
                or str(slots.get("payment_account_code") or "")
            )
            party = str(slots.get("party_name") or slots.get("full_name") or "").strip()
            raw_rows.append(
                {
                    "id": str(g.get("id") or ""),
                    "date": str(slots.get("txn_date") or str(g.get("resolved_at") or g.get("created_at") or "")[:10]),
                    "created_time": str(g.get("created_at") or ""),
                    "entry_type": mapped_type,
                    "direction": direction,
                    "amount_inr": round(float(slots.get("amount_inr") or 0), 2),
                    "account_name": "" if mapped_type == "bill" else acc_label,
                    "_account_id": "",
                    "category_or_offset": str(slots.get("expense_category") or mapped_type.replace("_", " ").title()),
                    "party_name": party,
                    "customer_name": party if mapped_type == "customer_receipt" else "",
                    "description": str(slots.get("notes") or g.get("summary_title") or ""),
                    "reference_number": str((steps[0].get("result") or {}).get("reference_number") if steps else ""),
                    "status": "draft" if mapped_type == "bill" else "completed",
                }
            )

    filtered: list[dict[str, Any]] = []
    for r in raw_rows:
        if et_clean and r["entry_type"] != et_clean:
            continue
        if account_needles or account_zoho_ids:
            if r["entry_type"] == "bill" and et_clean != "bill":
                continue
            acc_hay = r["account_name"].lower()
            if r["entry_type"] == "float_transfer":
                acc_hay = f"{acc_hay} {r['category_or_offset'].lower()}"
            if not (any(n in acc_hay for n in account_needles) or (r.get("_account_id") in account_zoho_ids)):
                continue
        if party_needles:
            p_hay = " ".join(
                [r["party_name"], r["customer_name"], r["description"], r["reference_number"], r["category_or_offset"]]
            ).lower()
            if not any(n in p_hay for n in party_needles):
                continue
        if date_from and r["date"] and r["date"] < date_from:
            continue
        if date_to and r["date"] and r["date"] > date_to:
            continue
        r.pop("_account_id", None)
        filtered.append(r)

    filtered.sort(key=lambda r: (r["date"], r["created_time"], r["id"]), reverse=True)
    return filtered[:clamped_limit]
