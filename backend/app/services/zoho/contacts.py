"""Zoho Books India contact search, creation, notes merge, and two-way sync."""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NON_PAYEE_WORDS: frozenset[str] = frozenset(
    {
        "expense",
        "other_expense",
        "vendor_payment",
        "customer_receipt",
        "float_transfer",
        "cash",
        "cash galla",
        "nakad",
        "chai",
        "tea",
        "nasta",
        "chai nasta",
        "petrol",
        "diesel",
        "auto",
        "tempo",
        "tempo bhada",
        "bhada",
        "freight",
        "rent",
        "kiraya",
        "bijli",
        "electricity",
        "water",
        "internet",
        "misc",
        "random",
        "workshop",
        "shop",
        "the furniture company",
        "the work app",
        "shreyansh jain",
    }
)


def _pkg() -> Any:
    return sys.modules[__package__]


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
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled or not name.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await pkg._zoho_request(
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
    pkg = _pkg()
    settings = pkg.get_settings()
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
            resp = await pkg._zoho_request(client, "POST", "/contacts", json_body=payload)
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
    pkg = _pkg()
    settings = pkg.get_settings()
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
                get_resp = await pkg._zoho_request(client, "GET", f"/contacts/{contact_id}")
                existing_notes = ""
                if get_resp.is_success:
                    existing_notes = (get_resp.json().get("contact") or {}).get("notes") or ""
                merged = merge_notes_text(existing_notes, notes_to_merge)
                if merged and merged != existing_notes.strip():
                    payload["notes"] = merged[:500]

            if not payload:
                return True

            put_resp = await pkg._zoho_request(
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
    return await _pkg().sync_existing_zoho_contact(contact_id, notes_to_merge=new_notes)


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
    """Resolve a Customer or Vendor against Zoho Books + entity_dictionary."""
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {"status": "stubbed", "contact_id": None, "contact_name": name}

    if selected_contact_id:
        cname = selected_contact_name or name
        pkg._upsert_entity_alias(sb, name, cname, contact_type, selected_contact_id, phone, notes=notes)
        if notes and notes.strip():
            await pkg.update_zoho_contact_notes(selected_contact_id, notes)
        return {"status": "resolved", "contact_id": selected_contact_id, "contact_name": cname}

    if not force_create:
        matches = await pkg.search_zoho_contacts(name, contact_type)
        if len(matches) == 1:
            m = matches[0]
            pkg._upsert_entity_alias(sb, name, m.get("contact_name") or name, contact_type, m["contact_id"], phone, notes=notes)
            if notes and notes.strip():
                await pkg.update_zoho_contact_notes(m["contact_id"], notes)
            return {"status": "resolved", "contact_id": m["contact_id"], "contact_name": m.get("contact_name") or name}
        if len(matches) >= 2:
            return {"status": "ambiguous", "matches": matches[:4]}

    created = await pkg.create_zoho_contact(name, contact_type, phone=phone, gstin=gstin, notes=notes)
    if not created.get("ok"):
        return {"status": "error", "error": created.get("error", "Failed creating Zoho contact")}
    c = created["contact"]
    cid = c.get("contact_id")
    cname = c.get("contact_name") or name
    pkg._upsert_entity_alias(sb, name, cname, contact_type, cid, phone, notes=notes)
    return {"status": "created", "contact_id": cid, "contact_name": cname}


async def resolve_payee_zoho_vendor(
    sb: Any,
    party_name: str | None,
    *,
    zoho_contact_id: str | None = None,
    allow_adhoc_create: bool = True,
) -> dict[str, Any]:
    """Resolve a payee (Vendor, Person/Karigar, or Payout Proxy Alias) to a Zoho Vendor Contact."""
    raw = (party_name or "").strip()
    if not zoho_contact_id and (not raw or raw.lower() in NON_PAYEE_WORDS):
        return {"contact_id": None, "canonical_name": "", "via_alias": None, "source": "none"}

    raw_low = raw.lower()
    matched_vendor: dict[str, Any] | None = None
    matched_person: dict[str, Any] | None = None
    source = "adhoc" if raw else "none"
    canonical_name = raw
    phone: str | None = None
    gstin: str | None = None
    cached_id: str | None = None

    if sb is not None and raw_low:
        vendors = (
            sb.table("vendors")
            .select("id,canonical_name,aliases,phone,gstin,zoho_contact_id,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        for v in vendors:
            names = [str(v.get("canonical_name") or ""), *(v.get("aliases") or [])]
            if any(str(n).strip().lower() == raw_low for n in names if str(n).strip()):
                matched_vendor = v
                source = "vendor"
                canonical_name = str(v.get("canonical_name") or raw).strip()
                phone = v.get("phone")
                gstin = v.get("gstin")
                cached_id = v.get("zoho_contact_id")
                break

        if not matched_vendor:
            people = (
                sb.table("people")
                .select("id,full_name,aliases,payout_aliases,phone,metadata,is_active")
                .eq("is_active", True)
                .execute()
                .data
                or []
            )
            for p in people:
                names = [
                    str(p.get("full_name") or ""),
                    *(p.get("aliases") or []),
                    *(p.get("payout_aliases") or []),
                ]
                if any(str(n).strip().lower() == raw_low for n in names if str(n).strip()):
                    matched_person = p
                    source = "person"
                    canonical_name = str(p.get("full_name") or raw).strip()
                    phone = p.get("phone")
                    cached_id = (p.get("metadata") or {}).get("zoho_contact_id")
                    break

    via_alias = raw if (raw and canonical_name and raw_low != canonical_name.lower()) else None
    cid = zoho_contact_id or cached_id
    err_msg: str | None = None

    if not cid and canonical_name and (source in ("vendor", "person") or allow_adhoc_create):
        c_res = await _pkg().resolve_or_create_contact(
            sb,
            canonical_name,
            "vendor",
            phone=phone,
            gstin=gstin,
        )
        cid = c_res.get("contact_id")
        if c_res.get("contact_name") and source == "adhoc":
            canonical_name = str(c_res["contact_name"])
        if c_res.get("status") == "error":
            err_msg = str(c_res.get("error") or "Failed resolving Zoho vendor contact")

    if cid and sb is not None:
        if matched_vendor and matched_vendor.get("zoho_contact_id") != cid:
            sb.table("vendors").update({"zoho_contact_id": cid}).eq("id", matched_vendor["id"]).execute()
        elif matched_person:
            meta = dict(matched_person.get("metadata") or {})
            if meta.get("zoho_contact_id") != cid:
                meta["zoho_contact_id"] = cid
                sb.table("people").update({"metadata": meta}).eq("id", matched_person["id"]).execute()

    out: dict[str, Any] = {
        "contact_id": cid,
        "canonical_name": canonical_name,
        "via_alias": via_alias,
        "source": source,
    }
    if err_msg:
        out["error"] = err_msg
    return out
