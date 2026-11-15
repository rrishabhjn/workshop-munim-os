import logging
import time
from typing import Any
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


async def _get_access_token(force_refresh: bool = False) -> str:
    """Exchange ZOHO_REFRESH_TOKEN for a short-lived Zoho India access token."""
    settings = get_settings()
    if not settings.zoho_enabled:
        raise RuntimeError("Zoho Books India credentials are not configured (Wave 2).")

    now = time.time()
    if (
        not force_refresh
        and _token_cache["access_token"]
        and now < _token_cache["expires_at"] - 60
    ):
        return str(_token_cache["access_token"])

    url = f"{settings.zoho_accounts_base}/oauth/v2/token"
    params = {
        "refresh_token": settings.zoho_refresh_token,
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in
    return access_token


async def _zoho_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Execute a Zoho Books India request with automatic 401 token-refresh retry."""
    settings = get_settings()
    merged_params = {"organization_id": settings.zoho_org_id, **(params or {})}
    url = f"{settings.zoho_books_api_base}/{path.lstrip('/')}"

    for attempt in range(2):
        token = await _get_access_token(force_refresh=(attempt == 1))
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = await client.request(
            method, url, headers=headers, params=merged_params, json=json_body
        )
        if resp.status_code != 401:
            return resp
    return resp


async def check_duplicate_bill(
    sb: Any,
    supplier_name: str,
    invoice_no: str | None,
    total_amount: float,
) -> dict[str, Any] | None:
    """Check historical_archive and Zoho Books for duplicate (Supplier + Invoice_No + Amount)."""
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

    settings = get_settings()
    if not settings.zoho_enabled or not invoice_no:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await _zoho_request(
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


def merge_notes_text(existing: str | None, addition: str | None) -> str:
    """Merge new fact/note into existing notes without duplicating identical clauses."""
    base = (existing or "").strip()
    add = (addition or "").strip()
    if not add:
        return base
    if not base:
        return add
    if add.lower() in base.lower():
        return base
    parts = [p.strip() for p in base.split(";") if p.strip()]
    if any(add.lower() == p.lower() for p in parts):
        return base
    return f"{base}; {add}"


def _upsert_entity_alias(
    sb: Any,
    alias_name: str,
    canonical_name: str,
    contact_type: str,
    zoho_contact_id: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
) -> None:
    """Save or update a customer/supplier mapping in Supabase entity_dictionary."""
    if sb is None or not canonical_name:
        return
    entity_type = "supplier" if contact_type == "vendor" else (
        "karigar" if contact_type == "karigar" else "customer"
    )
    existing = (
        sb.table("entity_dictionary")
        .select("id,aliases,zoho_contact_id,notes")
        .eq("entity_type", entity_type)
        .ilike("canonical_name", canonical_name)
        .limit(1)
        .execute()
        .data
    )
    alias_clean = alias_name.strip()
    if existing:
        row = existing[0]
        aliases = list(row.get("aliases") or [])
        if alias_clean and alias_clean.lower() != canonical_name.strip().lower() and alias_clean not in aliases:
            aliases.append(alias_clean)
        patch: dict[str, Any] = {"aliases": aliases}
        if zoho_contact_id:
            patch["zoho_contact_id"] = zoho_contact_id
        merged_notes = merge_notes_text(row.get("notes"), notes)
        if merged_notes and merged_notes != (row.get("notes") or ""):
            patch["notes"] = merged_notes
        sb.table("entity_dictionary").update(patch).eq("id", row["id"]).execute()
    else:
        aliases = [alias_clean] if alias_clean and alias_clean.lower() != canonical_name.strip().lower() else []
        payload: dict[str, Any] = {
            "canonical_name": canonical_name.strip(),
            "entity_type": entity_type,
            "aliases": aliases,
            "phone": phone,
            "zoho_contact_id": zoho_contact_id,
        }
        if notes and notes.strip():
            payload["notes"] = notes.strip()
        sb.table("entity_dictionary").insert(payload).execute()


async def search_zoho_contacts(name: str, contact_type: str) -> list[dict[str, Any]]:
    """Search Zoho Books contacts by name; return exact match alone if one exists, else up to 5."""
    settings = get_settings()
    if not settings.zoho_enabled or not name.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await _zoho_request(
                client,
                "GET",
                "/contacts",
                params={"contact_name_contains": name.strip(), "contact_type": contact_type},
            )
            if not resp.is_success:
                return []
            contacts = resp.json().get("contacts", [])
    except httpx.HTTPError as exc:
        logger.warning("Zoho search_zoho_contacts failed: %s", exc)
        return []

    target_lower = name.strip().lower()
    exact = [c for c in contacts if (c.get("contact_name") or "").strip().lower() == target_lower]
    if len(exact) == 1:
        return exact
    return contacts[:5]


async def create_zoho_contact(
    name: str,
    contact_type: str,
    *,
    phone: str | None = None,
    gstin: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a Customer or Vendor contact in Zoho Books India."""
    settings = get_settings()
    if not settings.zoho_enabled:
        return {"ok": False, "stubbed": True, "error": "Zoho not configured"}

    payload: dict[str, Any] = {
        "contact_name": name.strip(),
        "contact_type": contact_type,
    }
    if phone:
        payload["phone"] = phone
    if gstin:
        payload["gst_no"] = gstin.strip().upper()
        payload["gst_treatment"] = "business_gst"
    if notes and notes.strip():
        payload["notes"] = notes.strip()[:500]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await _zoho_request(client, "POST", "/contacts", json_body=payload)
            if not resp.is_success:
                return {"ok": False, "error": f"Zoho Contact error {resp.status_code}: {resp.text[:140]}"}
            return {"ok": True, "contact": resp.json().get("contact", {})}
    except httpx.HTTPError as exc:
        logger.warning("Zoho create_zoho_contact failed: %s", exc)
        return {"ok": False, "error": f"Zoho request failed: {exc}"}


async def update_zoho_contact_notes(contact_id: str, new_notes: str) -> bool:
    """Merge new_notes into an existing Zoho Books Contact's notes field (best-effort)."""
    settings = get_settings()
    if not settings.zoho_enabled or not contact_id or not new_notes.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            get_resp = await _zoho_request(client, "GET", f"/contacts/{contact_id}")
            existing_notes = ""
            if get_resp.is_success:
                existing_notes = (get_resp.json().get("contact") or {}).get("notes") or ""
            merged = merge_notes_text(existing_notes, new_notes)
            if not merged or merged == existing_notes.strip():
                return True
            put_resp = await _zoho_request(
                client,
                "PUT",
                f"/contacts/{contact_id}",
                json_body={"notes": merged[:500]},
            )
            return put_resp.is_success
    except httpx.HTTPError as exc:
        logger.warning("Zoho update_zoho_contact_notes failed: %s", exc)
        return False


async def resolve_or_create_contact(
    sb: Any,
    name: str,
    contact_type: str,
    *,
    phone: str | None = None,
    gstin: str | None = None,
    notes: str | None = None,
    force_create: bool = False,
    selected_contact_id: str | None = None,
    selected_contact_name: str | None = None,
) -> dict[str, Any]:
    """Resolve a Customer or Vendor against Zoho Books + entity_dictionary.

    Returns one of:
      - {"status": "resolved", "contact_id": ..., "contact_name": ...}
      - {"status": "created", "contact_id": ..., "contact_name": ...}
      - {"status": "ambiguous", "matches": [...]}
      - {"status": "error", "error": ...}
    """
    settings = get_settings()
    if not settings.zoho_enabled:
        return {"status": "stubbed", "contact_id": None, "contact_name": name}

    if selected_contact_id:
        cname = selected_contact_name or name
        _upsert_entity_alias(sb, name, cname, contact_type, selected_contact_id, phone, notes=notes)
        if notes and notes.strip():
            await update_zoho_contact_notes(selected_contact_id, notes)
        return {"status": "resolved", "contact_id": selected_contact_id, "contact_name": cname}

    if not force_create:
        matches = await search_zoho_contacts(name, contact_type)
        if len(matches) == 1:
            m = matches[0]
            _upsert_entity_alias(sb, name, m.get("contact_name") or name, contact_type, m["contact_id"], phone, notes=notes)
            if notes and notes.strip():
                await update_zoho_contact_notes(m["contact_id"], notes)
            return {"status": "resolved", "contact_id": m["contact_id"], "contact_name": m.get("contact_name") or name}
        if len(matches) >= 2:
            return {"status": "ambiguous", "matches": matches[:4]}

    created = await create_zoho_contact(name, contact_type, phone=phone, gstin=gstin, notes=notes)
    if not created.get("ok"):
        return {"status": "error", "error": created.get("error", "Failed creating Zoho contact")}
    c = created["contact"]
    cid = c.get("contact_id")
    cname = c.get("contact_name") or name
    _upsert_entity_alias(sb, name, cname, contact_type, cid, phone, notes=notes)
    return {"status": "created", "contact_id": cid, "contact_name": cname}


async def create_draft_bill(
    supplier_name: str,
    invoice_no: str | None,
    bill_date: str | None,
    line_items: list[dict[str, Any]],
    notes: str = "",
    vendor_id: str | None = None,
) -> dict[str, Any]:
    """Create a supplier bill strictly in DRAFT status in Zoho Books India.

    Automated code paths must NEVER finalize/approve bills or send customer invoices.
    """
    settings = get_settings()
    if not settings.zoho_enabled:
        return {
            "stubbed": True,
            "status": "draft",
            "message": "Zoho Books India not configured yet (Wave 2); bill logged locally.",
        }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if not vendor_id:
                c_resp = await _zoho_request(
                    client,
                    "GET",
                    "/contacts",
                    params={"contact_name": supplier_name, "contact_type": "vendor"},
                )
                if not c_resp.is_success:
                    return {"stubbed": False, "error": f"Zoho contacts error {c_resp.status_code}"}
                contacts = c_resp.json().get("contacts", [])
                if not contacts:
                    return {
                        "stubbed": False,
                        "error": f"Vendor '{supplier_name}' not found in Zoho Books.",
                    }
                vendor_id = contacts[0]["contact_id"]

            payload: dict[str, Any] = {
                "vendor_id": vendor_id,
                "status": "draft",  # HARDCODED DRAFT-ONLY BOUNDARY
                "bill_number": invoice_no or f"DRAFT-{int(time.time())}",
                "notes": f"[Workshop Munim Bot Draft] {notes}".strip(),
                "line_items": [
                    {
                        "name": li.get("description", "Material"),
                        "rate": li.get("rate", 0),
                        "quantity": li.get("quantity", 1),
                    }
                    for li in (line_items or [{"description": "Workshop Material", "rate": 0, "quantity": 1}])
                ],
            }
            if bill_date:
                payload["date"] = bill_date

            b_resp = await _zoho_request(client, "POST", "/bills", json_body=payload)
            if not b_resp.is_success:
                return {"stubbed": False, "error": f"Zoho Bill error {b_resp.status_code}: {b_resp.text[:160]}"}
            return {"stubbed": False, "bill": b_resp.json().get("bill", {})}
    except httpx.HTTPError as exc:
        logger.warning("Zoho create_draft_bill failed: %s", exc)
        return {"stubbed": False, "error": f"Zoho request failed: {exc}"}


async def sync_project_financial_caches(sb: Any) -> dict[str, Any]:
    """Nightly 02:00 IST refresh of zoho_invoiced_cache and zoho_bills_cache on active projects."""
    settings = get_settings()
    if not settings.zoho_enabled:
        return {"synced": 0, "stubbed": True, "reason": "Zoho credentials not configured"}

    token = await _get_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    projects = (
        sb.table("projects")
        .select("id,zoho_project_id")
        .eq("status", "active")
        .not_.is_("zoho_project_id", "null")
        .execute()
        .data
        or []
    )

    synced = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        for prj in projects:
            zp_id = prj["zoho_project_id"]
            inv_resp = await client.get(
                f"{settings.zoho_books_api_base}/invoices",
                headers=headers,
                params={"organization_id": settings.zoho_org_id, "project_id": zp_id},
            )
            bill_resp = await client.get(
                f"{settings.zoho_books_api_base}/bills",
                headers=headers,
                params={"organization_id": settings.zoho_org_id, "project_id": zp_id},
            )
            inv_total = sum(
                float(i.get("total", 0))
                for i in (inv_resp.json().get("invoices", []) if inv_resp.is_success else [])
                if i.get("status") != "void"
            )
            bill_total = sum(
                float(b.get("total", 0))
                for b in (bill_resp.json().get("bills", []) if bill_resp.is_success else [])
                if b.get("status") != "void"
            )
            sb.table("projects").update(
                {
                    "zoho_invoiced_cache": round(inv_total, 2),
                    "zoho_bills_cache": round(bill_total, 2),
                    "zoho_synced_at": "now()",
                }
            ).eq("id", prj["id"]).execute()
            synced += 1

    return {"synced": synced, "stubbed": False}


async def sync_cash_entry_to_zoho(
    txn_type: str,
    direction: str,
    party_or_karigar: str,
    amount_inr: float,
    txn_date: str | None,
    logged_by: str,
    notes: str = "",
    payment_mode: str = "cash",
    contact_id: str | None = None,
) -> dict[str, Any]:
    """Push a confirmed daily entry to Zoho Books India with the reporter's name.

    - Outflows (karigar_labor, local_transport, misc): creates a Zoho Expense (non-GST).
    - Supplier payments / customer receipts: creates Vendor/Customer Payment if contact
      exists in Zoho, or falls back to a Draft Manual Journal.
    """
    settings = get_settings()
    if not settings.zoho_enabled:
        return {"synced": False, "stubbed": True, "reason": "Zoho not configured"}

    tag = f"[Workshop Munim | By: {logged_by}]"
    desc = f"{tag} {party_or_karigar} — {notes}".strip()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Try Expense for wages / transport / overheads / misc outflows
            if direction == "out" and txn_type in ("karigar_labor", "local_transport", "overhead_expense", "misc"):
                coa_resp = await _zoho_request(client, "GET", "/chartofaccounts")
                if not coa_resp.is_success:
                    return {
                        "synced": False,
                        "error": f"Zoho COA error ({coa_resp.status_code}).",
                    }
                accounts = coa_resp.json().get("chartofaccounts", [])
                expense_acc = next(
                    (a["account_id"] for a in accounts if a.get("account_type") == "expense" and a.get("is_active")),
                    None,
                )
                pm_lower = (payment_mode or "cash").lower()
                is_bank = any(k in pm_lower for k in ("upi", "bank", "gpay", "phonepe", "paytm", "neft", "rtgs", "imps", "transfer"))
                preferred_types = ("bank", "cash") if is_bank else ("cash", "bank")
                paid_through = next(
                    (a["account_id"] for t in preferred_types for a in accounts if a.get("account_type") == t and a.get("is_active")),
                    None,
                )
                if not expense_acc or not paid_through:
                    return {"synced": False, "error": "Could not find active Expense/Cash account in Zoho."}

                exp_payload: dict[str, Any] = {
                    "account_id": expense_acc,
                    "paid_through_account_id": paid_through,
                    "amount": round(float(amount_inr), 2),
                    "reference_number": f"WM-{int(time.time())}",
                    "description": desc[:250],
                    "is_inclusive_tax": False,
                }
                if txn_date:
                    exp_payload["date"] = txn_date
                exp_resp = await _zoho_request(client, "POST", "/expenses", json_body=exp_payload)
                if not exp_resp.is_success:
                    return {"synced": False, "error": f"Zoho Expense error {exp_resp.status_code}: {exp_resp.text[:180]}"}
                exp_id = exp_resp.json().get("expense", {}).get("expense_id")
                return {"synced": True, "zoho_type": "expense", "zoho_id": exp_id}

            # 2. Customer receipt or supplier payment -> try Contact Payment
            cid = contact_id
            if not cid:
                contact_type = "customer" if direction == "in" else "vendor"
                c_resp = await _zoho_request(
                    client,
                    "GET",
                    "/contacts",
                    params={"contact_name": party_or_karigar, "contact_type": contact_type},
                )
                contacts = c_resp.json().get("contacts", []) if c_resp.is_success else []
                cid = contacts[0]["contact_id"] if contacts else None

            if cid:
                pm_lower = (payment_mode or "cash").lower()
                is_bank = any(k in pm_lower for k in ("upi", "bank", "gpay", "phonepe", "paytm", "neft", "rtgs", "imps", "transfer"))
                zoho_mode = "banktransfer" if is_bank else "cash"
                if direction == "in":
                    pay_resp = await _zoho_request(
                        client,
                        "POST",
                        "/customerpayments",
                        json_body={
                            "customer_id": cid,
                            "payment_mode": zoho_mode,
                            "amount": round(float(amount_inr), 2),
                            "reference_number": f"WM-{int(time.time())}",
                            "description": desc[:250],
                            **({"date": txn_date} if txn_date else {}),
                        },
                    )
                    if pay_resp.is_success:
                        pid = pay_resp.json().get("payment", {}).get("payment_id")
                        return {"synced": True, "zoho_type": "customer_payment", "zoho_id": pid}
                else:
                    vp_resp = await _zoho_request(
                        client,
                        "POST",
                        "/vendorpayments",
                        json_body={
                            "vendor_id": cid,
                            "payment_mode": zoho_mode,
                            "amount": round(float(amount_inr), 2),
                            "reference_number": f"WM-{int(time.time())}",
                            "description": desc[:250],
                            **({"date": txn_date} if txn_date else {}),
                        },
                    )
                    if vp_resp.is_success:
                        vpid = vp_resp.json().get("vendorpayment", {}).get("payment_id")
                        return {"synced": True, "zoho_type": "vendor_payment", "zoho_id": vpid}

            return {
                "synced": False,
                "error": f"Party '{party_or_karigar}' not matched in Zoho; kept in Supabase cash_ledger.",
            }
    except httpx.HTTPError as exc:
        logger.warning("Zoho sync_cash_entry_to_zoho failed: %s", exc)
        return {"synced": False, "error": f"Zoho request timed out or failed: {exc}"}
