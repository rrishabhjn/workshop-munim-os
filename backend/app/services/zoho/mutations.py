"""Zoho Books India mutation reversal helpers (same-day IST undo & amendments)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

from app.services.goal_primitives import classify_external_error

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


_ZOHO_DELETE_PATHS: dict[str, str] = {
    "expense": "/expenses/{id}",
    "transfer_fund": "/banktransactions/{id}",
    "transfer": "/banktransactions/{id}",
    "sales_deposit": "/banktransactions/{id}",
    "banktransaction": "/banktransactions/{id}",
    "customer_payment": "/customerpayments/{id}",
    "customerpayment": "/customerpayments/{id}",
    "vendor_payment": "/vendorpayments/{id}",
    "vendorpayment": "/vendorpayments/{id}",
    "bill": "/bills/{id}",
    "bill_open": "/bills/{id}",
    "journal": "/journals/{id}",
    "invoice": "/invoices/{id}",
}


async def delete_zoho_financial_entry(
    *, zoho_id: str | None, zoho_type: str | None
) -> dict[str, Any]:
    """Delete (or void as fallback) a Zoho Books entry created by AgentCore."""
    pkg = _pkg()
    settings = pkg.get_settings()
    zid = str(zoho_id or "").strip()
    ztype = str(zoho_type or "").strip().lower()

    if not settings.zoho_enabled or zid.startswith("STUB-"):
        return {
            "ok": True,
            "stubbed": True,
            "deleted": True,
            "zoho_id": zid,
            "zoho_type": ztype,
        }

    path_tmpl = _ZOHO_DELETE_PATHS.get(ztype)
    if not zid or not path_tmpl:
        return {
            "ok": False,
            "status_code": 400,
            "error_kind": "validation_400",
            "error": f"Cannot reverse Zoho entry: missing/unknown reference ({ztype or 'unknown'}: {zid or 'none'}).",
        }

    path = path_tmpl.format(id=zid)
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await pkg._zoho_request(client, "DELETE", path)
            if resp.is_success or resp.status_code == 404:
                return {
                    "ok": True,
                    "deleted": True,
                    "already_missing": resp.status_code == 404,
                    "zoho_id": zid,
                    "zoho_type": ztype,
                }
            if ztype in ("bill", "bill_open", "invoice"):
                doc_pl = "bills" if ztype.startswith("bill") else "invoices"
                void_path = f"/{doc_pl}/{zid}/status/void"
                v_resp = await pkg._zoho_request(client, "POST", void_path, json_body={})
                if v_resp.is_success:
                    return {
                        "ok": True,
                        "voided": True,
                        "zoho_id": zid,
                        "zoho_type": ztype,
                    }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_kind": classify_external_error(resp.status_code),
                "error": f"Zoho DELETE {path} failed ({resp.status_code}): {resp.text[:160]}",
            }
    except httpx.TimeoutException as exc:
        logger.warning("Zoho delete timed out for %s %s: %s", ztype, zid, exc)
        return {
            "ok": False,
            "status_code": None,
            "error_kind": "transient_5xx",
            "error": f"Zoho undo request timed out: {exc}",
        }
    except httpx.HTTPError as exc:
        logger.warning("Zoho delete HTTP error for %s %s: %s", ztype, zid, exc)
        return {
            "ok": False,
            "status_code": 503,
            "error_kind": "transient_5xx",
            "error": f"Zoho undo request failed: {exc}",
        }
