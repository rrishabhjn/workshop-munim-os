import logging
import re
import time
from typing import Any
import httpx

from app.config import get_settings
from app.services.goal_primitives import (
    build_zoho_standard_order_tags,
    classify_external_error,
)

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


async def sync_existing_zoho_contact(
    contact_id: str | None,
    *,
    name: str | None = None,
    phone: str | None = None,
    gstin: str | None = None,
    notes_to_merge: str | None = None,
    replace_notes: str | None = None,
) -> bool:
    """Sync edits onto an ALREADY linked Zoho Books Contact (lazy creation preserved)."""
    settings = get_settings()
    if not settings.zoho_enabled or not contact_id:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload: dict[str, Any] = {}
            if name and name.strip():
                payload["contact_name"] = name.strip()
            if phone is not None:
                payload["phone"] = phone.strip()
            if gstin is not None:
                clean_gst = gstin.strip().upper()
                payload["gst_no"] = clean_gst
                if clean_gst:
                    payload["gst_treatment"] = "business_gst"

            if replace_notes is not None:
                payload["notes"] = replace_notes.strip()[:500]
            elif notes_to_merge and notes_to_merge.strip():
                get_resp = await _zoho_request(client, "GET", f"/contacts/{contact_id}")
                existing_notes = ""
                if get_resp.is_success:
                    existing_notes = (get_resp.json().get("contact") or {}).get("notes") or ""
                merged = merge_notes_text(existing_notes, notes_to_merge)
                if merged and merged != existing_notes.strip():
                    payload["notes"] = merged[:500]

            if not payload:
                return True

            put_resp = await _zoho_request(
                client,
                "PUT",
                f"/contacts/{contact_id}",
                json_body=payload,
            )
            return put_resp.is_success
    except httpx.HTTPError as exc:
        logger.warning("Zoho sync_existing_zoho_contact failed for %s: %s", contact_id, exc)
        return False


async def update_zoho_contact_notes(contact_id: str, new_notes: str) -> bool:
    """Merge new_notes into an existing Zoho Books Contact's notes field (best-effort)."""
    return await sync_existing_zoho_contact(contact_id, notes_to_merge=new_notes)


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
    *,
    reference_number: str | None = None,
    project_id: str | None = None,
    sb: Any = None,
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

    order_tags: dict[str, Any] = {}
    bridge: dict[str, Any] = {}
    if project_id and sb is not None:
        bridge = await ensure_project_zoho_bridge(sb, project_id)
        order_tags = build_zoho_standard_order_tags(
            project_code=bridge.get("cf_project_code"),
            zoho_tag_id=bridge.get("zoho_tag_id") or settings.zoho_project_tag_id,
            zoho_tag_option_id=bridge.get("zoho_tag_option_id"),
            custom_field_api_name=settings.zoho_project_custom_field,
        )

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            if reference_number:
                existing_bill_id = await _find_existing_by_reference(
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
                c_resp = await _zoho_request(
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

            b_resp = await _post_zoho_with_order_tag_fallback(
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


def _extract_zoho_doc_project_keys(
    doc: dict[str, Any], cf_api_name: str = "cf_project_code"
) -> tuple[set[str], set[str], str]:
    """Extract (tag_option_ids, lowercase project codes, legacy zoho_project_id) from a Zoho doc."""
    tag_opt_ids: set[str] = set()
    codes: set[str] = set()

    for tag_entry in [*(doc.get("tags") or []), *(
        t for li in (doc.get("line_items") or []) for t in (li.get("tags") or [])
    )]:
        if isinstance(tag_entry, dict):
            if tag_entry.get("tag_option_id"):
                tag_opt_ids.add(str(tag_entry["tag_option_id"]).strip())
            opt_name = str(tag_entry.get("tag_option_name") or "").strip()
            if opt_name:
                codes.add(opt_name.split(" - ", 1)[0].strip().lower())

    for cf in doc.get("custom_fields") or []:
        if isinstance(cf, dict):
            api_n = str(cf.get("api_name") or cf.get("customfield_id") or "").strip()
            val = str(cf.get("value") or "").strip()
            if val and (api_n == cf_api_name or api_n == "cf_project_code"):
                codes.add(val.lower())

    cf_hash = doc.get("custom_field_hash") or {}
    if isinstance(cf_hash, dict):
        cf_val = cf_hash.get(cf_api_name) or cf_hash.get(f"{cf_api_name}_unformatted") or cf_hash.get("cf_project_code")
        if cf_val:
            codes.add(str(cf_val).strip().lower())

    zp_id = str(doc.get("project_id") or "").strip()
    return tag_opt_ids, codes, zp_id


async def _list_all_zoho_docs(
    client: httpx.AsyncClient, path: str, key: str, max_pages: int = 5
) -> tuple[bool, list[dict[str, Any]]]:
    """Fetch paginated Zoho Books invoices or bills safely."""
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = await _zoho_request(client, "GET", path, params={"page": page, "per_page": 200})
        if not resp.is_success:
            return False, rows
        data = resp.json()
        rows.extend(data.get(key, []) or [])
        has_more = bool((data.get("page_context") or {}).get("has_more_page"))
        if not has_more:
            break
    return True, rows


async def sync_project_financial_caches(sb: Any) -> dict[str, Any]:
    """Nightly 02:00 IST & manual refresh of zoho_invoiced_cache / zoho_bills_cache.

    Aggregates non-void Zoho invoices & bills per active project using Zoho Standard Plan
    Reporting Tags (zoho_tag_option_id) and cf_project_code, with legacy zoho_project_id fallback.
    """
    settings = get_settings()
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
            inv_ok, invoices = await _list_all_zoho_docs(client, "/invoices", "invoices")
            bill_ok, bills = await _list_all_zoho_docs(client, "/bills", "bills")
    except httpx.HTTPError as exc:
        logger.warning("sync_project_financial_caches failed: %s", exc)
        return {"synced": 0, "stubbed": False, "error": str(exc)}

    if not inv_ok and not bill_ok:
        return {"synced": 0, "stubbed": False, "error": "Failed to fetch Zoho invoices and bills"}

    cf_name = settings.zoho_project_custom_field
    inv_docs = [
        (doc, *_extract_zoho_doc_project_keys(doc, cf_name))
        for doc in invoices
        if doc.get("status") != "void"
    ]
    bill_docs = [
        (doc, *_extract_zoho_doc_project_keys(doc, cf_name))
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


# ===========================================================================
# Phase 2: Zoho Books Standard Plan (~₹749/mo, GST-Off Management Ledger)
#   - 1:1 Reporting Tag Option + Custom Field Order Auto-Bridge
#   - Deterministic reference_number Idempotency Lookup before Retry
#   - Multi-Account Instant Push (Banks, Cash Galla, Supervisor Floats)
#   - Draft-Only Supplier Bills & Owner Pulse Sync
# ===========================================================================


async def _find_existing_by_reference(
    client: httpx.AsyncClient,
    list_path: str,
    list_key: str,
    id_field: str,
    reference_number: str,
) -> str | None:
    """Query Zoho by reference_number before retrying so timeouts never double-post."""
    if not reference_number:
        return None
    try:
        resp = await _zoho_request(
            client, "GET", list_path, params={"reference_number": reference_number}
        )
        if not resp.is_success:
            return None
        rows = resp.json().get(list_key, []) or []
        for row in rows:
            if (row.get("reference_number") or "").strip() == reference_number:
                return str(row.get(id_field) or "")
    except httpx.HTTPError:
        return None
    return None


async def ensure_project_zoho_bridge(sb: Any, project_id: str) -> dict[str, Any]:
    """Smart Auto-Bridge on Zoho Standard Plan:
    Ensures Zoho Customer Contact + 1:1 Zoho 'Project' Reporting Tag option exist
    when an order converts (Advance In / Site Sizing / first financial entry).
    """
    prj_rows = (
        sb.table("projects")
        .select("id,project_code,customer_id,customer_name,phone,zoho_contact_id,zoho_tag_id,zoho_tag_option_id,cf_project_code")
        .eq("id", project_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not prj_rows:
        return {"ok": False, "error": "Project not found in Supabase."}
    prj = prj_rows[0]

    settings = get_settings()
    if not settings.zoho_enabled:
        return {
            "ok": True,
            "stubbed": True,
            "zoho_contact_id": prj.get("zoho_contact_id"),
            "zoho_tag_id": prj.get("zoho_tag_id"),
            "zoho_tag_option_id": prj.get("zoho_tag_option_id"),
            "cf_project_code": prj.get("cf_project_code") or prj["project_code"],
        }

    zoho_contact_id = prj.get("zoho_contact_id")
    cust_row: dict[str, Any] | None = None
    if prj.get("customer_id"):
        c_data = (
            sb.table("customers")
            .select("id,full_name,area_landmark,phone,zoho_contact_id")
            .eq("id", prj["customer_id"])
            .limit(1)
            .execute()
            .data
        )
        cust_row = c_data[0] if c_data else None
        if cust_row and cust_row.get("zoho_contact_id"):
            zoho_contact_id = cust_row["zoho_contact_id"]

    # 1. Ensure Zoho Customer Contact
    if not zoho_contact_id:
        display_name = prj["customer_name"]
        if cust_row and cust_row.get("area_landmark"):
            display_name = f"{cust_row['full_name']} ({cust_row['area_landmark']})"
        c_res = await resolve_or_create_contact(
            sb,
            display_name,
            "customer",
            phone=(cust_row or {}).get("phone") or prj.get("phone"),
        )
        if c_res.get("contact_id"):
            zoho_contact_id = c_res["contact_id"]
            if cust_row:
                sb.table("customers").update({"zoho_contact_id": zoho_contact_id}).eq(
                    "id", cust_row["id"]
                ).execute()

    # 2. Ensure Zoho Reporting Tag Option (Standard Plan replacement for Zoho Projects)
    tag_id = prj.get("zoho_tag_id") or settings.zoho_project_tag_id or None
    tag_option_id = prj.get("zoho_tag_option_id")
    option_label = f"{prj['project_code']} - {prj['customer_name']}"[:95]

    if not tag_option_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if not tag_id:
                    tags_resp = await _zoho_request(client, "GET", "/reportingtags")
                    if tags_resp.is_success:
                        tags_json = tags_resp.json()
                        all_tags = tags_json.get("reporting_tags") or tags_json.get("tags") or []
                        project_tag = next(
                            (
                                t
                                for t in all_tags
                                if t.get("is_active", True)
                                and "project" in (t.get("tag_name") or "").lower()
                            ),
                            None,
                        )
                        if project_tag and project_tag.get("tag_id"):
                            tag_id = str(project_tag["tag_id"])

                if tag_id:
                    tag_resp = await _zoho_request(client, "GET", f"/reportingtags/{tag_id}")
                    opts: list[dict[str, Any]] = []
                    if tag_resp.is_success:
                        tag_json = tag_resp.json()
                        tag_obj = tag_json.get("reporting_tag") or tag_json.get("tag") or tag_json
                        opts = list(tag_obj.get("tag_options") or tag_json.get("tag_options") or [])
                    if not opts:
                        opts_resp = await _zoho_request(client, "GET", f"/reportingtags/{tag_id}/options/all")
                        if opts_resp.is_success:
                            opts = list(opts_resp.json().get("tag_options") or [])

                    existing_opt = next(
                        (
                            o
                            for o in opts
                            if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                        ),
                        None,
                    )
                    if existing_opt:
                        tag_option_id = str(existing_opt.get("tag_option_id") or "") or None
                    else:
                        add_resp = await _zoho_request(
                            client,
                            "POST",
                            f"/reportingtags/{tag_id}/options",
                            json_body={"tag_option_name": option_label, "is_active": True},
                        )
                        if not add_resp.is_success:
                            merged_opts = [
                                {
                                    "tag_option_id": o.get("tag_option_id"),
                                    "tag_option_name": o.get("tag_option_name"),
                                    "is_active": o.get("is_active", True),
                                }
                                for o in opts
                                if o.get("tag_option_name")
                            ] + [{"tag_option_name": option_label, "is_active": True}]
                            add_resp = await _zoho_request(
                                client,
                                "PUT",
                                f"/reportingtags/{tag_id}/options",
                                json_body={"tag_options": merged_opts},
                            )
                        if add_resp.is_success:
                            add_json = add_resp.json()
                            created_opt = add_json.get("tag_option")
                            if not created_opt:
                                resp_opts = add_json.get("tag_options") or []
                                created_opt = next(
                                    (
                                        o
                                        for o in resp_opts
                                        if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                                    ),
                                    resp_opts[0] if resp_opts else {},
                                )
                            tag_option_id = str((created_opt or {}).get("tag_option_id") or "") or None
                            if not tag_option_id:
                                ref_resp = await _zoho_request(client, "GET", f"/reportingtags/{tag_id}")
                                if ref_resp.is_success:
                                    r_json = ref_resp.json()
                                    r_obj = r_json.get("reporting_tag") or r_json.get("tag") or r_json
                                    r_opts = r_obj.get("tag_options") or r_json.get("tag_options") or []
                                    matched = next(
                                        (
                                            o
                                            for o in r_opts
                                            if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                                        ),
                                        None,
                                    )
                                    if matched:
                                        tag_option_id = str(matched.get("tag_option_id") or "") or None
        except httpx.HTTPError as exc:
            logger.warning("Zoho Reporting Tag option auto-create skipped: %s", exc)

    patch: dict[str, Any] = {
        "cf_project_code": prj.get("cf_project_code") or prj["project_code"],
    }
    if zoho_contact_id:
        patch["zoho_contact_id"] = zoho_contact_id
    if tag_id:
        patch["zoho_tag_id"] = tag_id
    if tag_option_id:
        patch["zoho_tag_option_id"] = tag_option_id
    sb.table("projects").update(patch).eq("id", project_id).execute()

    return {
        "ok": True,
        "stubbed": False,
        "zoho_contact_id": zoho_contact_id,
        "zoho_tag_id": tag_id,
        "zoho_tag_option_id": tag_option_id,
        "cf_project_code": patch["cf_project_code"],
    }


def _pick_matching_zoho_bank_account(
    local_acc: dict[str, Any],
    zoho_accs: list[dict[str, Any]],
    claimed_zoho_ids: set[str],
) -> dict[str, Any] | None:
    """Safely match a local payment_account to a live Zoho bank/cash account.

    Prevents word-overlap bugs ('RGB' matching 'CCRGB') and protects Cash Wallets
    (supervisor_float) from binding to firm bank accounts or 'Cash Galla'/'Petty Cash'.
    """
    code = str(local_acc.get("code") or "").strip().lower()
    kind = str(local_acc.get("account_kind") or "").strip().lower()

    eligible: list[dict[str, Any]] = []
    for ba in zoho_accs:
        zid = str(ba.get("account_id") or "").strip()
        aname = (ba.get("account_name") or "").strip().lower()
        atype = (ba.get("account_type") or "").strip().lower()
        if not zid or zid in claimed_zoho_ids or ba.get("is_active") is False:
            continue
        if aname == "undeposited funds":
            continue
        if kind == "supervisor_float":
            if aname in ("petty cash", "cash galla", "cash"):
                continue
            if atype and atype != "cash" and ("wallet" not in aname and "float" not in aname):
                continue
        eligible.append(ba)

    if code == "cash_galla":
        for preferred in ("cash galla", "petty cash"):
            hit = next(
                (z for z in eligible if (z.get("account_name") or "").strip().lower() == preferred),
                None,
            )
            if hit:
                return hit

    candidates_raw = [
        str(local_acc.get("display_name") or ""),
        code,
        code.replace("_", " "),
        *[str(a) for a in (local_acc.get("aliases") or [])],
    ]
    candidates = [
        c.strip().lower()
        for c in candidates_raw
        if c and c.strip() and c.strip().lower() not in ("cash", "nakad")
    ]

    for cand in candidates:
        for ba in eligible:
            aname = (ba.get("account_name") or "").strip().lower()
            if aname == cand:
                return ba

    for cand in candidates:
        pat = re.compile(rf"\b{re.escape(cand)}\b")
        for ba in eligible:
            aname = (ba.get("account_name") or "").strip().lower()
            if pat.search(aname):
                return ba
    return None


async def _post_zoho_with_order_tag_fallback(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    has_order_tags: bool,
) -> httpx.Response:
    """POST to Zoho Books and retry once without custom_fields/tags on field-config 400."""
    resp = await _zoho_request(client, "POST", path, json_body=payload)
    if not resp.is_success and resp.status_code == 400 and has_order_tags:
        err_low = resp.text.lower()
        if any(k in err_low for k in ("custom", "field", "tag")):
            fallback_payload = {
                k: v for k, v in payload.items() if k not in ("custom_fields", "tags")
            }
            if isinstance(fallback_payload.get("line_items"), list):
                fallback_payload["line_items"] = [
                    {lk: lv for lk, lv in li.items() if lk != "tags"}
                    if isinstance(li, dict)
                    else li
                    for li in fallback_payload["line_items"]
                ]
            resp = await _zoho_request(client, "POST", path, json_body=fallback_payload)
    return resp


async def _resolve_zoho_paid_account_id(
    client: httpx.AsyncClient, sb: Any, payment_account_row: dict[str, Any]
) -> str | None:
    """Resolve Zoho Books account_id for one of the canonical payment_accounts."""
    if payment_account_row.get("zoho_account_id"):
        return str(payment_account_row["zoho_account_id"])

    resp = await _zoho_request(client, "GET", "/bankaccounts")
    if resp.is_success:
        b_accs = resp.json().get("bankaccounts") or []
        all_local = sb.table("payment_accounts").select("id,zoho_account_id").eq("is_active", True).execute().data or []
        claimed_ids = {
            str(r["zoho_account_id"])
            for r in all_local
            if r.get("zoho_account_id") and str(r.get("id")) != str(payment_account_row.get("id"))
        }
        match = _pick_matching_zoho_bank_account(payment_account_row, b_accs, claimed_ids)
        if match:
            zid = str(match["account_id"])
            sb.table("payment_accounts").update({"zoho_account_id": zid}).eq(
                "id", payment_account_row["id"]
            ).execute()
            return zid
    return None


async def push_management_entry_to_zoho(
    sb: Any,
    *,
    entry_kind: str,  # 'customer_receipt' | 'vendor_payment' | 'expense' | 'float_transfer'
    amount_inr: float,
    payment_account_id: str,
    to_payment_account_id: str | None = None,
    reference_number: str,
    txn_date: str | None = None,
    party_name: str = "",
    zoho_contact_id: str | None = None,
    project_id: str | None = None,
    notes: str = "",
    actor_label: str = "Munim",
) -> dict[str, Any]:
    """Push a confirmed management ledger transaction to Zoho Books India (GST-off Standard Plan)
    with reference_number idempotency and 1:1 Project Reporting Tag linking.
    """
    settings = get_settings()
    acc_rows = (
        sb.table("payment_accounts")
        .select("*")
        .eq("id", payment_account_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not acc_rows:
        return {
            "ok": False,
            "status_code": 400,
            "error_kind": "validation_400",
            "error": {"message": "Payment account is required (e.g., Cash Galla, Axis, Float)."},
        }
    from_acc = acc_rows[0]

    bridge: dict[str, Any] = {}
    if project_id:
        bridge = await ensure_project_zoho_bridge(sb, project_id)
        if not zoho_contact_id and entry_kind == "customer_receipt":
            zoho_contact_id = bridge.get("zoho_contact_id")

    if not settings.zoho_enabled:
        return {
            "ok": True,
            "stubbed": True,
            "status_code": 200,
            "error_kind": "none",
            "zoho_type": entry_kind,
            "zoho_id": f"STUB-{reference_number}",
            "account_name": from_acc["display_name"],
        }

    order_tags = build_zoho_standard_order_tags(
        project_code=bridge.get("cf_project_code"),
        zoho_tag_id=bridge.get("zoho_tag_id") or settings.zoho_project_tag_id,
        zoho_tag_option_id=bridge.get("zoho_tag_option_id"),
        custom_field_api_name=settings.zoho_project_custom_field,
    )
    proj_prefix = f"[{bridge['cf_project_code']}] " if bridge.get("cf_project_code") else ""
    desc = f"[{actor_label}] {proj_prefix}{party_name} {notes}".strip()[:250]

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            from_zoho_acc = await _resolve_zoho_paid_account_id(client, sb, from_acc)
            if not from_zoho_acc:
                return {
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {
                        "message": f"Account '{from_acc['display_name']}' is not yet mapped to a Zoho Books Bank/Cash/Petty-Cash account."
                    },
                }

            if entry_kind == "float_transfer":
                if not to_payment_account_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": "Destination account/float is required for a transfer."},
                    }
                to_rows = (
                    sb.table("payment_accounts")
                    .select("*")
                    .eq("id", to_payment_account_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                to_zoho_acc = (
                    await _resolve_zoho_paid_account_id(client, sb, to_rows[0])
                    if to_rows
                    else None
                )
                if not to_zoho_acc:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": "Destination Zoho account is not mapped yet."},
                    }
                existing_id = await _find_existing_by_reference(
                    client, "/banktransactions", "banktransactions", "transaction_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "transfer_fund",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                payload = {
                    "transaction_type": "transfer_fund",
                    "from_account_id": from_zoho_acc,
                    "to_account_id": to_zoho_acc,
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": desc,
                    **({"date": txn_date} if txn_date else {}),
                }
                resp = await _zoho_request(client, "POST", "/banktransactions", json_body=payload)
                if resp.is_success:
                    tid = (resp.json().get("banktransaction") or {}).get("transaction_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "transfer_fund",
                        "zoho_id": tid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            if entry_kind == "customer_receipt":
                existing_id = await _find_existing_by_reference(
                    client, "/customerpayments", "customerpayments", "payment_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "customer_payment",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                if not zoho_contact_id and party_name:
                    c_res = await resolve_or_create_contact(sb, party_name, "customer")
                    zoho_contact_id = c_res.get("contact_id")
                if not zoho_contact_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": f"Customer '{party_name}' could not be resolved in Zoho Books."},
                    }
                payload = {
                    "customer_id": zoho_contact_id,
                    "account_id": from_zoho_acc,
                    "payment_mode": "cash" if from_acc["account_kind"] in ("cash_galla", "supervisor_float") else "banktransfer",
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": desc,
                    **({"date": txn_date} if txn_date else {}),
                    **order_tags,
                }
                resp = await _post_zoho_with_order_tag_fallback(
                    client, "/customerpayments", payload, has_order_tags=bool(order_tags)
                )
                if resp.is_success:
                    pid = (resp.json().get("payment") or {}).get("payment_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "customer_payment",
                        "zoho_id": pid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            if entry_kind == "vendor_payment":
                existing_id = await _find_existing_by_reference(
                    client, "/vendorpayments", "vendorpayments", "payment_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "vendor_payment",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                if not zoho_contact_id and party_name:
                    c_res = await resolve_or_create_contact(sb, party_name, "vendor")
                    zoho_contact_id = c_res.get("contact_id")
                if not zoho_contact_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": f"Vendor '{party_name}' could not be resolved in Zoho Books."},
                    }
                payload = {
                    "vendor_id": zoho_contact_id,
                    "paid_through_account_id": from_zoho_acc,
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": desc,
                    **({"date": txn_date} if txn_date else {}),
                    **order_tags,
                }
                resp = await _post_zoho_with_order_tag_fallback(
                    client, "/vendorpayments", payload, has_order_tags=bool(order_tags)
                )
                if resp.is_success:
                    vpid = (resp.json().get("vendorpayment") or {}).get("payment_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "vendor_payment",
                        "zoho_id": vpid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            # Default: non-GST Management Expense
            existing_id = await _find_existing_by_reference(
                client, "/expenses", "expenses", "expense_id", reference_number
            )
            if existing_id:
                return {
                    "ok": True,
                    "status_code": 200,
                    "error_kind": "none",
                    "zoho_type": "expense",
                    "zoho_id": existing_id,
                    "deduped": True,
                }
            coa_resp = await _zoho_request(client, "GET", "/chartofaccounts")
            accounts = coa_resp.json().get("chartofaccounts", []) if coa_resp.is_success else []
            expense_acc = next(
                (a["account_id"] for a in accounts if a.get("account_type") in ("expense", "cost_of_goods_sold") and a.get("is_active")),
                None,
            )
            if not expense_acc:
                return {
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {"message": "No active Expense account found in Zoho Chart of Accounts."},
                }
            exp_payload = {
                "account_id": expense_acc,
                "paid_through_account_id": from_zoho_acc,
                "amount": round(float(amount_inr), 2),
                "reference_number": reference_number,
                "description": desc,
                "is_inclusive_tax": False,
                **({"date": txn_date} if txn_date else {}),
                **order_tags,
            }
            resp = await _post_zoho_with_order_tag_fallback(
                client, "/expenses", exp_payload, has_order_tags=bool(order_tags)
            )
            if resp.is_success:
                eid = (resp.json().get("expense") or {}).get("expense_id")
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "error_kind": "none",
                    "zoho_type": "expense",
                    "zoho_id": eid,
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_kind": classify_external_error(resp.status_code),
                "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
            }
    except httpx.TimeoutException as exc:
        return {
            "ok": False,
            "status_code": None,
            "error_kind": "transient_5xx",
            "error": {"message": f"Zoho timeout: {exc}"},
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 503,
            "error_kind": "transient_5xx",
            "error": {"message": f"Zoho network error: {exc}"},
        }


async def sync_owner_pulse_snapshot(sb: Any) -> dict[str, Any]:
    """Refresh Zoho Bank/Cash/Float balances into payment_accounts.cached_balance_inr."""
    settings = get_settings()
    if not settings.zoho_enabled:
        return {"ok": True, "stubbed": True, "accounts_synced": 0}

    synced = 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await _zoho_request(client, "GET", "/bankaccounts")
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
                    match = _pick_matching_zoho_bank_account(la, zoho_accs, claimed_zoho_ids)
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

