"""Zoho Books India duplicate bill check and strictly DRAFT-only supplier bill creation."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import httpx

from app.services.goal_primitives import build_zoho_standard_order_tags, classify_external_error

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


async def _resolve_doc_order_tags(
    sb: Any, project_id: str | None, settings: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not (project_id and sb is not None):
        return {}, {}
    bridge = await _pkg().ensure_project_zoho_bridge(sb, project_id)
    order_tags = build_zoho_standard_order_tags(
        project_code=bridge.get("cf_project_code"),
        zoho_tag_id=bridge.get("zoho_tag_id") or settings.zoho_project_tag_id,
        zoho_tag_option_id=bridge.get("zoho_tag_option_id"),
        custom_field_api_name=settings.zoho_project_custom_field,
    )
    return bridge, order_tags


async def check_duplicate_bill(
    sb: Any,
    supplier_name: str,
    invoice_no: str | None,
    total_amount: float,
) -> dict[str, Any] | None:
    """Check historical_archive and Zoho Books for duplicate (Supplier + Invoice_No + Amount)."""
    pkg = _pkg()
    if invoice_no:
        arch = (
            sb.table("historical_archive")
            .select("id,party_name,invoice_no,amount_inr,doc_date")
            .ilike("party_name", f"%{supplier_name}%")
            .eq("invoice_no", invoice_no)
            .limit(1)
            .execute()
        )
        if arch.data:
            return {"source": "historical_archive", "match": arch.data[0]}

    settings = pkg.get_settings()
    if not settings.zoho_enabled or not invoice_no:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await pkg._zoho_request(
                client, "GET", "/bills", params={"bill_number": invoice_no}
            )
            if not resp.is_success:
                logger.warning("Zoho duplicate check returned %s", resp.status_code)
                return None
            bills = resp.json().get("bills", [])
    except httpx.HTTPError as exc:
        logger.warning("Zoho duplicate check failed: %s", exc)
        return None

    for b in bills:
        vendor_match = supplier_name.lower() in (b.get("vendor_name") or "").lower()
        amt_match = abs(float(b.get("total", 0)) - total_amount) < 1.0
        if vendor_match or amt_match:
            return {"source": "zoho_books", "match": b}

    return None


async def create_draft_bill(
    supplier_name: str,
    invoice_no: str | None,
    bill_date: str | None,
    line_items: list[dict[str, Any]],
    notes: str = "",
    vendor_id: str | None = None,
    *,
    reference_number: str | None = None,
    project_id: str | None = None,
    sb: Any = None,
) -> dict[str, Any]:
    """Create a supplier bill strictly in DRAFT status in Zoho Books India.

    Automated code paths must NEVER finalize/approve bills or send customer invoices.
    """
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {
            "stubbed": True,
            "status": "draft",
            "message": "Zoho Books India not configured yet (Wave 2); bill logged locally.",
        }

    order_tags: dict[str, Any] = {}
    bridge: dict[str, Any] = {}
    bridge, order_tags = await _resolve_doc_order_tags(sb, project_id, settings)

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if reference_number:
                existing_bill_id = await pkg._find_existing_by_reference(
                    client, "/bills", "bills", "bill_id", reference_number
                )
                if existing_bill_id:
                    return {
                        "stubbed": False,
                        "deduped": True,
                        "bill": {
                            "bill_id": existing_bill_id,
                            "status": "draft",
                            "reference_number": reference_number,
                        },
                    }

            if not vendor_id:
                c_resp = await pkg._zoho_request(
                    client,
                    "GET",
                    "/contacts",
                    params={"contact_name": supplier_name, "contact_type": "vendor"},
                )
                if not c_resp.is_success:
                    return {
                        "stubbed": False,
                        "status_code": c_resp.status_code,
                        "error_kind": classify_external_error(c_resp.status_code),
                        "error": f"Zoho contacts error {c_resp.status_code}",
                    }
                contacts = c_resp.json().get("contacts", [])
                if not contacts:
                    return {
                        "stubbed": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": f"Vendor '{supplier_name}' not found in Zoho Books.",
                    }
                vendor_id = contacts[0]["contact_id"]

            line_tags = {"tags": order_tags["tags"]} if order_tags.get("tags") else {}
            proj_prefix = f"[{bridge['cf_project_code']}] " if bridge.get("cf_project_code") else ""
            payload: dict[str, Any] = {
                "vendor_id": vendor_id,
                "status": "draft",  # HARDCODED DRAFT-ONLY BOUNDARY
                "bill_number": invoice_no or f"DRAFT-{int(time.time())}",
                "notes": f"[Workshop Munim Bot Draft] {proj_prefix}{notes}".strip(),
                "line_items": [
                    {
                        "name": li.get("description", "Material"),
                        "rate": li.get("rate", 0),
                        "quantity": li.get("quantity", 1),
                        **line_tags,
                    }
                    for li in (line_items or [{"description": "Workshop Material", "rate": 0, "quantity": 1}])
                ],
                **order_tags,
            }
            if reference_number:
                payload["reference_number"] = reference_number
            if bill_date:
                payload["date"] = bill_date

            b_resp = await pkg._post_zoho_with_order_tag_fallback(
                client, "/bills", payload, has_order_tags=bool(order_tags)
            )
            if not b_resp.is_success:
                return {
                    "stubbed": False,
                    "status_code": b_resp.status_code,
                    "error_kind": classify_external_error(b_resp.status_code),
                    "error": f"Zoho Bill error {b_resp.status_code}: {b_resp.text[:160]}",
                }
            return {"stubbed": False, "bill": b_resp.json().get("bill", {})}
    except httpx.TimeoutException as exc:
        logger.warning("Zoho create_draft_bill timed out: %s", exc)
        return {
            "stubbed": False,
            "status_code": None,
            "error_kind": "transient_5xx",
            "error": f"Zoho request timed out: {exc}",
        }
    except httpx.HTTPError as exc:
        logger.warning("Zoho create_draft_bill failed: %s", exc)
        return {
            "stubbed": False,
            "status_code": 503,
            "error_kind": "transient_5xx",
            "error": f"Zoho request failed: {exc}",
        }


async def create_draft_invoice(
    customer_name: str,
    invoice_date: str | None,
    line_items: list[dict[str, Any]],
    notes: str = "",
    customer_zoho_id: str | None = None,
    *,
    reference_number: str | None = None,
    project_id: str | None = None,
    sb: Any = None,
) -> dict[str, Any]:
    """Create a customer receivable invoice strictly in DRAFT status in Zoho Books India.

    Automated code paths must NEVER finalize or email invoices directly to customers.
    """
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {
            "stubbed": True,
            "status": "draft",
            "message": "Zoho Books India not configured; receivable draft invoice logged locally.",
        }

    bridge, order_tags = await _resolve_doc_order_tags(sb, project_id, settings)
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if reference_number:
                existing_inv_id = await pkg._find_existing_by_reference(
                    client, "/invoices", "invoices", "invoice_id", reference_number
                )
                if existing_inv_id:
                    return {
                        "stubbed": False,
                        "deduped": True,
                        "invoice": {
                            "invoice_id": existing_inv_id,
                            "status": "draft",
                            "reference_number": reference_number,
                        },
                    }

            cust_id = customer_zoho_id or bridge.get("zoho_contact_id")
            if not cust_id and sb is not None:
                c_res = await pkg.resolve_or_create_contact(sb, customer_name, "customer")
                cust_id = c_res.get("contact_id")
            if not cust_id:
                return {
                    "stubbed": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": f"Customer '{customer_name}' could not be resolved in Zoho Books.",
                }

            line_tags = {"tags": order_tags["tags"]} if order_tags.get("tags") else {}
            proj_prefix = f"[{bridge['cf_project_code']}] " if bridge.get("cf_project_code") else ""
            payload: dict[str, Any] = {
                "customer_id": cust_id,
                "status": "draft",  # HARDCODED DRAFT-ONLY BOUNDARY
                "is_inclusive_tax": False,
                "notes": f"[Workshop Munim Bot Draft] {proj_prefix}{notes}".strip(),
                "line_items": [
                    {
                        "name": str(li.get("description") or "Custom Furniture / Interior Work")[:95],
                        "rate": li.get("rate", 0),
                        "quantity": li.get("quantity", 1),
                        **line_tags,
                    }
                    for li in (
                        line_items
                        or [{"description": "Custom Furniture / Interior Work", "rate": 0, "quantity": 1}]
                    )
                ],
                **order_tags,
            }
            if reference_number:
                payload["reference_number"] = reference_number
            if invoice_date:
                payload["date"] = invoice_date

            inv_resp = await pkg._post_zoho_with_order_tag_fallback(
                client, "/invoices", payload, has_order_tags=bool(order_tags)
            )
            if not inv_resp.is_success:
                return {
                    "stubbed": False,
                    "status_code": inv_resp.status_code,
                    "error_kind": classify_external_error(inv_resp.status_code),
                    "error": f"Zoho Invoice error {inv_resp.status_code}: {inv_resp.text[:160]}",
                }
            return {"stubbed": False, "invoice": inv_resp.json().get("invoice", {})}
    except httpx.TimeoutException as exc:
        logger.warning("Zoho create_draft_invoice timed out: %s", exc)
        return {
            "stubbed": False,
            "status_code": None,
            "error_kind": "transient_5xx",
            "error": f"Zoho request timed out: {exc}",
        }
    except httpx.HTTPError as exc:
        logger.warning("Zoho create_draft_invoice failed: %s", exc)
        return {
            "stubbed": False,
            "status_code": 503,
            "error_kind": "transient_5xx",
            "error": f"Zoho request failed: {exc}",
        }
