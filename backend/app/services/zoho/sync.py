"""Nightly & manual Zoho Books India project P&L cache sync and Owner Pulse snapshot sync."""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

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
