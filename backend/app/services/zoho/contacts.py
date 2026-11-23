"""Zoho Books India contact search, creation, notes merge, and two-way sync."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HONORIFIC_STOPWORDS: frozenset[str] = frozenset({"ji", "bhaiya", "bhai", "sir", "mam", "madam", "mr", "mrs"})
ROLE_STOPWORDS: frozenset[str] = _HONORIFIC_STOPWORDS | frozenset(
    {
        "mistri",
        "karigar",
        "painter",
        "polish",
        "polisher",
        "carpenter",
        "suthar",
        "cushion",
        "cushioning",
        "sofa",
        "electrician",
        "plumber",
        "fabricator",
        "welder",
        "contractor",
        "thekedar",
        "helper",
    }
)

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
        "food",
        "lunch",
        "dinner",
        "mess",
        "nashta",
        "fuel",
        "petrol",
        "diesel",
        "auto",
        "tempo",
        "tempo bhada",
        "transport",
        "bhada",
        "freight",
        "rent",
        "kiraya",
        "bijli",
        "electricity",
        "water",
        "internet",
        "misc",
        "miscellaneous",
        "factory expense",
        "shop expense",
        "office expense",
        "stationery",
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
    suffixed_lower = f"{target_lower} ({contact_type.strip().lower()})"
    exact = [
        c
        for c in contacts
        if (c.get("contact_name") or "").strip().lower() in (target_lower, suffixed_lower)
    ]
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
            if not resp.is_success and resp.status_code == 400 and (
                "already exists" in resp.text.lower() or "3062" in resp.text
            ):
                # Zoho shares contact_name uniqueness across customers and vendors; auto-suffix on collision
                suffix = " (Customer)" if contact_type == "customer" else " (Vendor)"
                clean_name = name.strip()
                disambiguated_name = (
                    clean_name if clean_name.lower().endswith(suffix.lower()) else f"{clean_name}{suffix}"
                )
                if disambiguated_name != payload["contact_name"]:
                    retry_payload = {**payload, "contact_name": disambiguated_name}
                    resp = await pkg._zoho_request(client, "POST", "/contacts", json_body=retry_payload)
                    if not resp.is_success and resp.status_code == 400 and (
                        "already exists" in resp.text.lower() or "3062" in resp.text
                    ):
                        lookup_resp = await pkg._zoho_request(
                            client,
                            "GET",
                            "/contacts",
                            params={"contact_name_contains": disambiguated_name, "contact_type": contact_type},
                        )
                        if lookup_resp.is_success:
                            for existing in lookup_resp.json().get("contacts", []):
                                if (existing.get("contact_name") or "").strip().lower() == disambiguated_name.lower():
                                    return {"ok": True, "contact": existing}
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


def _core_name_tokens(raw: str | None) -> tuple[str, ...]:
    tokens = re.findall(r"[a-z0-9]+", (raw or "").strip().lower())
    return tuple(t for t in tokens if len(t) > 1 and t not in ROLE_STOPWORDS)


async def match_existing_payee(sb: Any, raw_name: str | None) -> dict[str, Any]:
    """Read-only deterministic matcher across Supabase vendors, people, payout_aliases & Zoho Vendors."""
    raw = (raw_name or "").strip()
    if not raw or raw.lower() in NON_PAYEE_WORDS:
        return {"status": "none"}

    raw_low = raw.lower()
    core_tokens = _core_name_tokens(raw)
    pool: list[dict[str, Any]] = []

    if sb is not None:
        for v in sb.table("vendors").select("id,canonical_name,aliases,specialty,zoho_contact_id,is_active").eq("is_active", True).execute().data or []:
            cname = str(v.get("canonical_name") or "").strip()
            if not cname:
                continue
            spec = str(v.get("specialty") or "").strip()
            all_names = [cname, *(str(a).strip() for a in (v.get("aliases") or []) if str(a).strip())]
            pool.append(
                {
                    "canonical_name": cname,
                    "vendor_id": str(v["id"]),
                    "person_id": None,
                    "zoho_contact_id": str(v["zoho_contact_id"]) if v.get("zoho_contact_id") else None,
                    "source": "vendor",
                    "source_label": f"Vendor{' · ' + spec if spec else ''}",
                    "all_names": all_names,
                    "role_blob": " ".join([*all_names, spec]).lower(),
                }
            )
        for p in sb.table("people").select("id,full_name,person_type,role_tags,aliases,payout_aliases,metadata,is_active").eq("is_active", True).execute().data or []:
            fname = str(p.get("full_name") or "").strip()
            if not fname:
                continue
            all_names = [
                fname,
                *(str(a).strip() for a in (p.get("aliases") or []) if str(a).strip()),
                *(str(pa).strip() for pa in (p.get("payout_aliases") or []) if str(pa).strip()),
            ]
            meta = p.get("metadata") or {}
            pool.append(
                {
                    "canonical_name": fname,
                    "vendor_id": None,
                    "person_id": str(p["id"]),
                    "zoho_contact_id": str(meta["zoho_contact_id"]) if meta.get("zoho_contact_id") else None,
                    "source": "person",
                    "source_label": str(p.get("person_type") or "Person"),
                    "all_names": all_names,
                    "role_blob": " ".join([*all_names, *(str(rt) for rt in (p.get("role_tags") or []))]).lower(),
                }
            )

    pkg = _pkg()
    if pkg.get_settings().zoho_enabled and core_tokens:
        z_hits = await pkg.search_zoho_contacts(" ".join(core_tokens), "vendor")
        for z in z_hits:
            zid = str(z.get("contact_id") or "").strip()
            zname = str(z.get("contact_name") or "").strip()
            if not zname:
                continue
            existing = next(
                (
                    it
                    for it in pool
                    if (zid and it["zoho_contact_id"] == zid)
                    or it["canonical_name"].lower() == zname.lower()
                ),
                None,
            )
            if existing:
                if zid and not existing["zoho_contact_id"]:
                    existing["zoho_contact_id"] = zid
                continue
            pool.append(
                {
                    "canonical_name": zname,
                    "vendor_id": None,
                    "person_id": None,
                    "zoho_contact_id": zid or None,
                    "source": "zoho",
                    "source_label": "Zoho Vendor",
                    "all_names": [zname],
                    "role_blob": zname.lower(),
                }
            )

    def _to_matched(it: dict[str, Any], role_hint: str | None = None) -> dict[str, Any]:
        via = raw if raw_low != it["canonical_name"].lower() and not role_hint else None
        return {
            "status": "matched",
            "canonical_name": it["canonical_name"],
            "zoho_contact_id": it["zoho_contact_id"],
            "vendor_id": it["vendor_id"],
            "person_id": it["person_id"],
            "via_alias": via,
            "source": it["source"],
            "role_hint": role_hint,
        }

    exact_hits = [it for it in pool if any(n.lower() == raw_low for n in it["all_names"])]
    if len(exact_hits) == 1:
        return _to_matched(exact_hits[0])

    candidates = exact_hits if len(exact_hits) >= 2 else []
    raw_roles = set(re.findall(r"[a-z0-9]+", raw_low)) & (ROLE_STOPWORDS - _HONORIFIC_STOPWORDS)
    if not candidates and core_tokens:
        for it in pool:
            for nm in it["all_names"]:
                c_toks = _core_name_tokens(nm)
                if c_toks == core_tokens or (len(c_toks) >= len(core_tokens) and c_toks[: len(core_tokens)] == core_tokens):
                    candidates.append(it)
                    break

    if len(candidates) > 1 and raw_roles:
        narrowed = [it for it in candidates if any(rk in it["role_blob"] for rk in raw_roles)]
        if narrowed:
            candidates = narrowed

    if len(candidates) == 1:
        hint = ", ".join(sorted(raw_roles)).title() if raw_roles else None
        return _to_matched(candidates[0], role_hint=hint)

    if len(candidates) >= 2:
        choices: list[dict[str, Any]] = []
        for idx, it in enumerate(candidates[:3]):
            patch_obj: dict[str, Any] = {
                "party_name": it["canonical_name"],
                "is_ambiguous": False,
                "is_new_payee": False,
            }
            if it["zoho_contact_id"]:
                patch_obj["zoho_contact_id"] = it["zoho_contact_id"]
            if it["vendor_id"]:
                patch_obj["vendor_id"] = it["vendor_id"]
            if it["person_id"]:
                patch_obj["person_id"] = it["person_id"]
            choices.append(
                {
                    "choice_id": f"payee_{idx}",
                    "label": f"{it['canonical_name']} ({it['source_label']})"[:42],
                    "slot_patch_json": json.dumps(patch_obj, ensure_ascii=False),
                }
            )
        choices.append(
            {
                "choice_id": "payee_new",
                "label": f"🆕 New: {raw[:32]}",
                "slot_patch_json": json.dumps({"party_name": raw, "is_ambiguous": False, "is_new_payee": True}, ensure_ascii=False),
            }
        )
        return {
            "status": "ambiguous",
            "choices": choices,
            "question": f"Multiple payees match *{raw}* — kaunsa select karein?",
        }

    return {"status": "new", "canonical_name": raw}


def find_matching_active_orders(sb: Any, hint: str | None) -> list[dict[str, Any]]:
    """Find active Supabase customer orders matching project code, customer/alias, or city hint."""
    raw = (hint or "").strip().lstrip("#").strip()
    if sb is None or not raw:
        return []

    projects = (
        sb.table("projects")
        .select("id,project_code,customer_id,customer_name,site_area_landmark,status")
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    if not projects:
        return []

    low = raw.lower()
    code_hits = [p for p in projects if str(p.get("project_code") or "").strip().lower() == low]
    if code_hits:
        return code_hits

    cust_rows = (
        sb.table("customers")
        .select("id,full_name,aliases,area_landmark")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    cust_by_id = {str(c["id"]): c for c in cust_rows if c.get("id")}

    def _name_variants(p: dict[str, Any]) -> list[str]:
        names = [str(p.get("customer_name") or "").strip()]
        c = cust_by_id.get(str(p.get("customer_id") or ""))
        if c:
            if c.get("full_name"):
                names.append(str(c["full_name"]).strip())
            names.extend(str(a).strip() for a in (c.get("aliases") or []) if str(a).strip())
        return [n for n in names if n]

    exact_hits = [p for p in projects if any(n.lower() == low for n in _name_variants(p))]
    if exact_hits:
        return exact_hits

    core = _core_name_tokens(raw)
    if not core:
        return []
    raw_set = set(core)
    tok_hits: list[dict[str, Any]] = []
    area_narrowed: list[dict[str, Any]] = []

    for p in projects:
        c = cust_by_id.get(str(p.get("customer_id") or ""))
        area_str = str(p.get("site_area_landmark") or (c or {}).get("area_landmark") or "")
        area_toks = set(_core_name_tokens(area_str))
        query_toks = [t for t in core if t not in area_toks]
        if not query_toks:
            continue
        q_set = set(query_toks)
        variants = _name_variants(p)
        if any(q_set.issubset(set(_core_name_tokens(v))) for v in variants):
            tok_hits.append(p)
            if area_toks and (raw_set & area_toks):
                area_narrowed.append(p)

    return area_narrowed if area_narrowed else tok_hits


def match_existing_order_or_customer(sb: Any, hint: str | None) -> dict[str, Any]:
    """Read-only deterministic matcher for active Supabase customer orders."""
    raw = (hint or "").strip().lstrip("#").strip()
    if sb is None or not raw:
        return {"status": "none"}

    hits = find_matching_active_orders(sb, raw)
    if len(hits) == 1:
        p = hits[0]
        return {
            "status": "matched",
            "project_id": p["id"],
            "customer_id": p.get("customer_id"),
            "customer_name": p.get("customer_name"),
        }

    if len(hits) >= 2:
        choices = []
        for idx, p in enumerate(hits[:4]):
            area = f" ({p['site_area_landmark']})" if p.get("site_area_landmark") else ""
            lbl = f"#{p.get('project_code') or ''} {p.get('customer_name')}{area}".strip()[:42]
            patch_obj = {"project_id": p["id"], "is_ambiguous": False}
            if p.get("customer_id"):
                patch_obj["customer_id"] = p["customer_id"]
            if p.get("customer_name"):
                patch_obj["party_name"] = p["customer_name"]
            choices.append(
                {
                    "choice_id": f"order_{idx}",
                    "label": lbl,
                    "slot_patch_json": json.dumps(patch_obj, ensure_ascii=False),
                }
            )
        return {
            "status": "ambiguous",
            "choices": choices,
            "question": f"Multiple active orders match *{raw}* — kaunsa order select karein?",
        }
    return {"status": "none"}


async def match_existing_customer(sb: Any, raw_name: str | None) -> dict[str, Any]:
    """Read-only deterministic matcher across Supabase customers & Zoho Customers."""
    raw = (raw_name or "").strip()
    if not raw or raw.lower() in NON_PAYEE_WORDS:
        return {"status": "none"}

    raw_low = raw.lower()
    core_tokens = _core_name_tokens(raw)
    pool: list[dict[str, Any]] = []

    if sb is not None:
        for c in (
            sb.table("customers")
            .select("id,full_name,area_landmark,aliases,zoho_contact_id,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        ):
            fname = str(c.get("full_name") or "").strip()
            if not fname:
                continue
            area = str(c.get("area_landmark") or "").strip()
            all_names = [fname, *(str(a).strip() for a in (c.get("aliases") or []) if str(a).strip())]
            pool.append(
                {
                    "canonical_name": fname,
                    "customer_id": str(c["id"]),
                    "zoho_contact_id": str(c["zoho_contact_id"]) if c.get("zoho_contact_id") else None,
                    "area": area,
                    "all_names": all_names,
                }
            )

    pkg = _pkg()
    if pkg.get_settings().zoho_enabled and core_tokens:
        z_hits = await pkg.search_zoho_contacts(" ".join(core_tokens), "customer")
        for z in z_hits:
            zid = str(z.get("contact_id") or "").strip()
            zname = str(z.get("contact_name") or "").strip()
            if not zname:
                continue
            existing = next(
                (
                    it
                    for it in pool
                    if (zid and it["zoho_contact_id"] == zid)
                    or it["canonical_name"].lower() == zname.lower()
                ),
                None,
            )
            if existing:
                if zid and not existing["zoho_contact_id"]:
                    existing["zoho_contact_id"] = zid
                continue
            pool.append(
                {
                    "canonical_name": zname,
                    "customer_id": None,
                    "zoho_contact_id": zid or None,
                    "area": "",
                    "all_names": [zname],
                }
            )

    def _to_matched(it: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "matched",
            "canonical_name": it["canonical_name"],
            "customer_id": it["customer_id"],
            "zoho_contact_id": it["zoho_contact_id"],
        }

    exact_hits = [it for it in pool if any(n.lower() == raw_low for n in it["all_names"])]
    if len(exact_hits) == 1:
        return _to_matched(exact_hits[0])

    candidates = exact_hits if len(exact_hits) >= 2 else []
    if not candidates and core_tokens:
        for it in pool:
            for nm in it["all_names"]:
                c_toks = _core_name_tokens(nm)
                if c_toks == core_tokens or (len(c_toks) >= len(core_tokens) and c_toks[: len(core_tokens)] == core_tokens):
                    candidates.append(it)
                    break

    if len(candidates) == 1:
        return _to_matched(candidates[0])

    if len(candidates) >= 2:
        choices: list[dict[str, Any]] = []
        for idx, it in enumerate(candidates[:3]):
            area_tag = f" ({it['area']})" if it.get("area") else ""
            patch_obj: dict[str, Any] = {
                "party_name": it["canonical_name"],
                "is_ambiguous": False,
                "is_new_payee": False,
            }
            if it["customer_id"]:
                patch_obj["customer_id"] = it["customer_id"]
            if it["zoho_contact_id"]:
                patch_obj["zoho_contact_id"] = it["zoho_contact_id"]
            choices.append(
                {
                    "choice_id": f"cust_{idx}",
                    "label": f"{it['canonical_name']}{area_tag}"[:42],
                    "slot_patch_json": json.dumps(patch_obj, ensure_ascii=False),
                }
            )
        choices.append(
            {
                "choice_id": "cust_new",
                "label": f"🆕 New: {raw[:32]}",
                "slot_patch_json": json.dumps({"party_name": raw, "is_ambiguous": False, "is_new_payee": True}, ensure_ascii=False),
            }
        )
        return {
            "status": "ambiguous",
            "choices": choices,
            "question": f"Multiple customers match *{raw}* — kaunsa select karein?",
        }

    return {"status": "new", "canonical_name": raw}
