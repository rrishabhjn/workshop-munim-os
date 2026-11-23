"""Deterministic slot backfills, entity/UUID resolution, and quick-pick choice builders."""

from __future__ import annotations

import json
import re
from typing import Any
import uuid

from app.services import inventory_ai, step_executors
from app.services.agent_core.schemas import _NULL_STRINGS
from app.services.goal_primitives import (
    MATERIAL_TRACK_STAGES,
    compute_missing_customer_onboarding_slots,
    normalize_financial_entry_kind,
    normalize_or_infer_expense_category,
)

_MAT_UNIT_RE = (
    r"(sheets?|pcs?|pieces?|mtrs?|meters?|sqft|sq\.?ft|rft|kg|kgs|ltrs?|liters?|litres?|box(?:es)?|sets?|rolls?)"
)

_NON_AREA_PARENS: frozenset[str] = frozenset(
    {
        "lead", "new lead", "execution", "design", "estimating",
        "completed", "material to order", "check stock", "ordered", "in shop", "packed",
    }
)


def _map_op_to_db_goal_type(op: str) -> str:
    mapping = {
        "new_lead": "new_lead",
        "add_customer": "entity_onboarding",
        "update_customer": "entity_onboarding",
        "update_vendor": "entity_onboarding",
        "update_person": "entity_onboarding",
        "update_order_details": "stage_update",
        "archive_record": "entity_onboarding",
        "pipeline_stage_move": "stage_update",
        "material_stage_move": "stage_update",
        "item_upsert_or_move": "stage_update",
        "item_material_upsert": "stage_update",
        "customer_receipt": "financial_txn",
        "customer_payment": "financial_txn",
        "vendor_payment": "financial_txn",
        "expense": "financial_txn",
        "float_transfer": "float_transfer",
        "supplier_draft_bill": "supplier_bill",
        "customer_draft_invoice": "financial_txn",
        "add_person_or_vendor": "entity_onboarding",
        "inventory_load": "inventory_load", "inventory_attribution": "inventory_attribution", "inventory_count_adjust": "inventory_adjust",
    }
    return mapping.get(op, "stage_update")


def _fetch_target_row_for_slots(sb: Any, op: str, slots: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch existing Supabase record for Before->After diffing & sensitivity checks."""
    table_and_id: tuple[str, str | None] | None = None
    if op == "update_customer":
        table_and_id = ("customers", slots.get("customer_id"))
    elif op == "update_vendor":
        table_and_id = ("vendors", slots.get("vendor_id"))
    elif op == "update_person":
        table_and_id = ("people", slots.get("person_id"))
    elif op in ("update_order_details", "customer_receipt", "customer_payment", "customer_draft_invoice"):
        table_and_id = ("projects", slots.get("project_id"))
    elif op == "archive_record":
        kind = slots.get("archive_entity_kind")
        if kind == "customer":
            table_and_id = ("customers", slots.get("customer_id"))
        elif kind == "vendor":
            table_and_id = ("vendors", slots.get("vendor_id"))
        elif kind == "person":
            table_and_id = ("people", slots.get("person_id"))
        elif kind == "order":
            table_and_id = ("projects", slots.get("project_id"))
    if not table_and_id or not table_and_id[1]:
        return None
    rows = sb.table(table_and_id[0]).select("*").eq("id", table_and_id[1]).limit(1).execute().data or []
    return rows[0] if rows else None


_AMT_PREFIX_BLOCK_RE = re.compile(r"(?:\b(?:room|sector|flat|shop|plot)|#)\s*$", re.IGNORECASE)
_AMT_SUFFIX_BLOCK_RE = re.compile(
    r"\s*(?:mm|cm|ft|feet|inch|in\b|sqft|sq\.?ft|rft|mtrs?|meters?|kg|kgs|ltrs?|liters?|litres?|pcs?|pieces?|sheets?|box(?:es)?|rolls?|sets?|bhk|rooms?|%|am\b|pm\b|days?|hrs?|hours?)\b",
    re.IGNORECASE,
)


def _extract_inr_amount_from_text(*texts: str | None) -> float | None:
    """Deterministic fallback to extract an INR amount (e.g. ₹25, 50k, 1.5L, 20,000) without grabbing specs/room numbers."""
    for raw in texts:
        txt = (raw or "").strip()
        if not txt:
            continue
        cleaned = re.sub(r"\bPRJ-[A-Z0-9-]+\b", " ", txt, flags=re.IGNORECASE)
        for m_cur in re.finditer(r"(?:₹|rs\.?|inr)\s*(\d[\d,]*(?:\.\d{1,2})?)\b(?!\s*(?:lakh|lac|l|k)\b)", cleaned, re.IGNORECASE):
            try:
                val = round(float(m_cur.group(1).replace(",", "")), 2)
                if val > 0:
                    return val
            except ValueError:
                continue
        m_lakh = re.search(r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)\b", cleaned, re.IGNORECASE)
        if m_lakh:
            val = round(float(m_lakh.group(1)) * 100000.0, 2)
            if val > 0:
                return val
        m_k = re.search(r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*k\b", cleaned, re.IGNORECASE)
        if m_k:
            val = round(float(m_k.group(1)) * 1000.0, 2)
            if val > 0:
                return val
        for m_num in re.finditer(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d{1,2})?)(?![A-Za-z0-9])", cleaned):
            if _AMT_PREFIX_BLOCK_RE.search(cleaned[: m_num.start()]) or _AMT_SUFFIX_BLOCK_RE.match(cleaned[m_num.end() :]):
                continue
            try:
                val = round(float(m_num.group(1).replace(",", "")), 2)
                if val >= 50:
                    return val
            except ValueError:
                continue
    return None


def _extract_payment_account_code_from_text(sb: Any, *texts: str | None) -> str | None:
    """Deterministic 3-tier priority fallback to resolve payment_accounts without requiring 'wallet'."""
    combined = " ".join((t or "").strip().lower() for t in texts if t and t.strip())
    if not combined:
        return None
    accounts = (
        sb.table("payment_accounts")
        .select("code,display_name,account_kind,aliases")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    tier1: list[tuple[str, str]] = []
    tier2: list[tuple[str, str]] = []
    tier3: list[tuple[str, str]] = []

    for a in accounts:
        code = str(a.get("code") or "").strip()
        kind = str(a.get("account_kind") or "").strip()
        if not code:
            continue
        tokens = [
            code.replace("_", " "),
            str(a.get("display_name") or ""),
            *(a.get("aliases") or []),
        ]
        for tok in tokens:
            t_low = str(tok).strip().lower()
            if len(t_low) < 2:
                continue
            if kind == "cash_galla" and t_low in ("cash", "nakad"):
                tier3.append((t_low, code))
            elif kind == "supervisor_float" and " " not in t_low:
                tier2.append((t_low, code))
            else:
                tier1.append((t_low, code))

    for bucket in (tier1, tier2, tier3):
        bucket.sort(key=lambda pair: len(pair[0]), reverse=True)
        for tok, code in bucket:
            if re.search(rf"\b{re.escape(tok)}\b", combined):
                return code
    return None


def _match_person_kind_for_party(
    sb: Any, party_name: str | None
) -> str | None:
    """Return person_type ('salaried_karigar' / 'contractor_mistri') if party_name matches a non-vendor worker."""
    pname = (party_name or "").strip().lower()
    if not pname:
        return None
    try:
        vendors = (
            sb.table("vendors")
            .select("canonical_name,aliases,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        for v in vendors:
            v_names = [
                str(v.get("canonical_name") or "").strip().lower(),
                *[str(a).strip().lower() for a in (v.get("aliases") or [])],
            ]
            if any(vn and vn == pname for vn in v_names):
                return None

        people = (
            sb.table("people")
            .select("full_name,person_type,aliases,payout_aliases,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        for p in people:
            p_names = [
                str(p.get("full_name") or "").strip().lower(),
                *[str(a).strip().lower() for a in (p.get("aliases") or [])],
                *[str(pa).strip().lower() for pa in (p.get("payout_aliases") or [])],
            ]
            if any(pn and pn == pname for pn in p_names):
                return str(p.get("person_type") or "")
    except Exception:
        return None
    return None


def _backfill_financial_slots_from_text(
    sb: Any,
    slots: dict[str, Any],
    *,
    user_text: str | None,
    heard_transcript: str | None,
) -> None:
    """Ensure amount_inr, payment_account_code, expense_category, and party_name are populated cleanly."""
    op = str(slots.get("operation_kind") or "")
    if op == "new_lead" and float(slots.get("advance_amount_inr") or 0) > 0:
        if not slots.get("payment_account_code"):
            acc_code = _extract_payment_account_code_from_text(
                sb, user_text, heard_transcript, slots.get("summary_title"), slots.get("advance_notes")
            )
            if acc_code:
                slots["payment_account_code"] = acc_code
        return
    if op not in (
        "customer_receipt",
        "customer_payment",
        "vendor_payment",
        "expense",
        "float_transfer",
        "supplier_draft_bill",
        "customer_draft_invoice",
    ):
        return
    if slots.get("amount_inr") in (None, "", 0, 0.0):
        amt = _extract_inr_amount_from_text(user_text, heard_transcript, slots.get("summary_title"))
        if amt is not None:
            slots["amount_inr"] = amt
    if not slots.get("payment_account_code") and op not in ("supplier_draft_bill", "customer_draft_invoice"):
        acc_code = _extract_payment_account_code_from_text(
            sb, user_text, heard_transcript, slots.get("summary_title")
        )
        if acc_code:
            slots["payment_account_code"] = acc_code
    if not slots.get("party_name") and slots.get("project_id") and op in ("customer_receipt", "customer_payment", "customer_draft_invoice"):
        p_rows = (
            sb.table("projects")
            .select("customer_name")
            .eq("id", slots["project_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        if p_rows and p_rows[0].get("customer_name"):
            slots["party_name"] = p_rows[0]["customer_name"]
    if not slots.get("party_name"):
        for col, tbl, ncol in (("person_id", "people", "full_name"), ("vendor_id", "vendors", "canonical_name"), ("customer_id", "customers", "full_name")):
            if slots.get(col) and _accept_id(sb, slots[col], tbl):
                r = sb.table(tbl).select(ncol).eq("id", slots[col]).limit(1).execute().data or []
                if r and r[0].get(ncol):
                    slots["party_name"] = r[0][ncol]
                    break
    if not slots.get("party_name") and op in ("customer_receipt", "customer_payment", "customer_draft_invoice"):
        for raw_txt in (user_text, heard_transcript, slots.get("summary_title")):
            m_buy = re.search(
                r"\b(?:sales?\s+to|sold\s+to|from|via)\s+([a-z][a-z\s]{1,32}?)(?=\s+(?:for|in|via|on|ka|ki|ke|se|rs|₹|\d|owes)|$)",
                str(raw_txt or ""),
                re.IGNORECASE,
            )
            if m_buy:
                slots["party_name"] = " ".join(m_buy.group(1).split()).title()
                break

    op = normalize_financial_entry_kind(op, user_text, heard_transcript, slots.get("notes"), slots.get("summary_title"))
    slots["operation_kind"] = op

    matched_person_kind = _match_person_kind_for_party(sb, slots.get("party_name"))
    combined_cues = " ".join(
        str(x).strip().lower()
        for x in (
            user_text,
            heard_transcript,
            slots.get("notes"),
            slots.get("summary_title"),
        )
        if x and str(x).strip()
    )
    has_salary_kw = bool(
        re.search(
            r"\b(salary|salaries|tankhwah|tankha|pagar|wages|wage|dihadi|fnf|bonus)\b",
            combined_cues,
        )
    )

    if op == "vendor_payment" and (
        matched_person_kind == "salaried_karigar"
        or slots.get("expense_category") == "salary_wages"
        or has_salary_kw
    ):
        slots["operation_kind"] = "expense"
        op = "expense"
        slots["expense_category"] = "salary_wages"

    if op == "expense":
        cat = normalize_or_infer_expense_category(
            slots.get("expense_category"),
            user_text,
            heard_transcript,
            slots.get("notes"),
            slots.get("summary_title"),
            slots.get("party_name"),
        )
        if cat == "other_expense":
            if matched_person_kind == "salaried_karigar":
                cat = "salary_wages"
            elif matched_person_kind == "contractor_mistri":
                cat = "contractor_labor"
        slots["expense_category"] = cat


def _is_valid_uuid(val: Any) -> bool:
    if val is None:
        return False
    try:
        uuid.UUID(str(val).strip())
        return True
    except (ValueError, AttributeError):
        return False


def _is_fake_db(sb: Any) -> bool:
    return hasattr(sb, "store")


def _accept_id(sb: Any, val: Any, table: str | None = None) -> bool:
    if val is None or str(val).strip().lower() in _NULL_STRINGS:
        return False
    if _is_valid_uuid(val):
        return True
    if _is_fake_db(sb) and not getattr(sb, "strict_uuids", False):
        if table is None:
            return True
        return any(str(r.get("id")) == str(val) for r in (sb.store.get(table) or []))
    return False


def _resolve_person_id_by_name(sb: Any, raw: str) -> str | None:
    needle = (raw or "").strip().lower()
    if not needle or needle in _NULL_STRINGS:
        return None
    rows = (
        sb.table("people")
        .select("id,full_name,aliases,is_active")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    exact: list[str] = []
    partial: list[str] = []
    for r in rows:
        names = [str(r.get("full_name") or "").strip().lower(), *(str(a).strip().lower() for a in (r.get("aliases") or []))]
        if any(n == needle for n in names if n):
            exact.append(str(r["id"]))
        elif any(needle in n or n in needle for n in names if n):
            partial.append(str(r["id"]))
    if len(exact) == 1:
        return exact[0]
    if not exact and len(partial) == 1:
        return partial[0]
    return None


def _resolve_project_id_by_hint(sb: Any, raw: str | None) -> str | None:
    hint = (raw or "").strip().lstrip("#").strip()
    if not hint or hint.lower() in _NULL_STRINGS:
        return None
    rows = (
        sb.table("projects")
        .select("id,project_code,customer_name,status")
        .execute()
        .data
        or []
    )
    low = hint.lower()
    code_matches = [str(r["id"]) for r in rows if str(r.get("project_code") or "").strip().lower() == low]
    if len(code_matches) == 1:
        return code_matches[0]
    active_rows = [r for r in rows if r.get("status", "active") == "active"] or rows
    exact_cust = [str(r["id"]) for r in active_rows if str(r.get("customer_name") or "").strip().lower() == low]
    if len(exact_cust) == 1:
        return exact_cust[0]
    partial_cust = [str(r["id"]) for r in active_rows if low in str(r.get("customer_name") or "").strip().lower()]
    if len(partial_cust) == 1:
        return partial_cust[0]
    return None


def _resolve_entity_id_by_name(sb: Any, table: str, name_col: str, raw: str | None) -> str | None:
    hint = (raw or "").strip()
    if not hint or hint.lower() in _NULL_STRINGS:
        return None
    rows = (
        sb.table(table)
        .select(f"id,{name_col},aliases,is_active")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    low = hint.lower()
    exact: list[str] = []
    for r in rows:
        candidates = [str(r.get(name_col) or "").strip().lower(), *(str(a).strip().lower() for a in (r.get("aliases") or []))]
        if any(c == low for c in candidates if c):
            exact.append(str(r["id"]))
    if len(exact) == 1:
        return exact[0]
    return None


def _sanitize_and_resolve_slot_ids(sb: Any, slots: dict[str, Any]) -> None:
    """Resolve accidental project codes / human names in UUID slots and normalize dates/stages."""
    op = str(slots.get("operation_kind") or "")
    for eid_col, tbl in (("vendor_id", "vendors"), ("customer_id", "customers"), ("person_id", "people")):
        ev = slots.get(eid_col)
        if sb is not None and ev and _is_valid_uuid(ev):
            if not (sb.table(tbl).select("id").eq("id", ev).limit(1).execute().data or []) and (sb.table("projects").select("id").eq("id", ev).limit(1).execute().data or []):
                slots.pop(eid_col, None)

    pid = slots.get("project_id")
    if pid is not None and not _accept_id(sb, pid, "projects"):
        resolved_pid = _resolve_project_id_by_hint(sb, str(pid))
        if resolved_pid:
            slots["project_id"] = resolved_pid
        else:
            slots.pop("project_id", None)
    if not slots.get("project_id") and op in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "update_order_details",
        "customer_draft_invoice",
        "inventory_attribution",
    ):
        hint = slots.get("party_name") or slots.get("full_name")
        resolved_pid = _resolve_project_id_by_hint(sb, hint)
        if resolved_pid:
            slots["project_id"] = resolved_pid

    iid = slots.get("item_id")
    if iid is not None and not _accept_id(sb, iid, "project_items"):
        raw_item = str(iid).strip()
        if raw_item and not slots.get("item_name"):
            slots["item_name"] = raw_item
        slots.pop("item_id", None)

    for col in ("production_mistri_id", "assembly_mistri_id", "sizing_mistri_id", "person_id"):
        val = slots.get(col)
        if val is not None and not _accept_id(sb, val, "people"):
            resolved = _resolve_person_id_by_name(sb, str(val))
            if resolved:
                slots[col] = resolved
            else:
                slots.pop(col, None)

    if isinstance(slots.get("estimator_ids"), list):
        resolved_ests: list[str] = []
        for entry in slots["estimator_ids"]:
            if _accept_id(sb, entry, "people"):
                resolved_ests.append(str(entry))
            else:
                r_id = _resolve_person_id_by_name(sb, str(entry))
                if r_id:
                    resolved_ests.append(r_id)
        slots["estimator_ids"] = resolved_ests

    cid = slots.get("customer_id")
    if cid is not None and not _accept_id(sb, cid, "customers"):
        resolved_cid = _resolve_entity_id_by_name(sb, "customers", "full_name", str(cid))
        if resolved_cid:
            slots["customer_id"] = resolved_cid
        else:
            raw_c = str(cid).strip()
            if raw_c and not slots.get("full_name") and not slots.get("party_name"):
                slots["full_name"] = raw_c
            slots.pop("customer_id", None)

    vid = slots.get("vendor_id")
    if vid is not None and not _accept_id(sb, vid, "vendors"):
        resolved_vid = _resolve_entity_id_by_name(sb, "vendors", "canonical_name", str(vid))
        if resolved_vid:
            slots["vendor_id"] = resolved_vid
        else:
            raw_v = str(vid).strip()
            if raw_v and not slots.get("party_name"):
                slots["party_name"] = raw_v
            slots.pop("vendor_id", None)

    if slots.get("to_stage"):
        norm_st = step_executors.normalize_stage_code_for_op(op, str(slots["to_stage"]))
        if norm_st:
            slots["to_stage"] = norm_st
    if slots.get("material_stage"):
        norm_mat = step_executors.normalize_stage_code_for_op("material_stage_move", str(slots["material_stage"]))
        if norm_mat in MATERIAL_TRACK_STAGES:
            slots["material_stage"] = norm_mat
        else:
            slots.pop("material_stage", None)
    if slots.get("material_status"):
        slots["material_status"] = step_executors.normalize_material_status(str(slots["material_status"]))
    for dcol in ("promised_delivery_date", "txn_date"):
        if dcol in slots and slots[dcol] is not None:
            clean_d = step_executors.sanitize_iso_date(slots[dcol])
            if clean_d:
                slots[dcol] = clean_d
            else:
                slots.pop(dcol, None)

    if slots.get("payment_account_code") or slots.get("to_payment_account_code"):
        acc_rows = (
            sb.table("payment_accounts")
            .select("id,code")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        code_map = {
            str(a["code"]).strip().lower(): (str(a["code"]), str(a["id"]))
            for a in acc_rows
            if a.get("code") and a.get("id")
        }
        if slots.get("payment_account_code"):
            c_key = str(slots["payment_account_code"]).strip().lower()
            if c_key in code_map:
                slots["payment_account_code"], slots["payment_account_id"] = code_map[c_key]
        if slots.get("to_payment_account_code"):
            tc_key = str(slots["to_payment_account_code"]).strip().lower()
            if tc_key in code_map:
                slots["to_payment_account_code"], slots["to_payment_account_id"] = code_map[tc_key]
    inventory_ai.resolve_inventory_slots(sb, slots, _resolve_project_id_by_hint)


def _backfill_material_slots_from_text(
    slots: dict[str, Any],
    *,
    user_text: str | None,
    heard_transcript: str | None,
) -> None:
    """Deterministically extract material_lines_json / material_status if LLM put details only in summary/text."""
    if str(slots.get("operation_kind") or "") != "item_material_upsert":
        return
    if not slots.get("material_lines_json"):
        extracted: list[dict[str, Any]] = []
        pattern = re.compile(
            rf"(\d+(?:\.\d+)?)\s*{_MAT_UNIT_RE}\s+(?:of\s+)?([A-Za-z0-9][A-Za-z0-9\s./+-]{{1,48}}?)"
            r"(?=\s+(?:to|in|on|for|mein|me|ke|ki|ka|aur|and)\b|$|,|;)",
            re.IGNORECASE,
        )
        for src_text in (user_text, heard_transcript, slots.get("summary_title")):
            txt = (src_text or "").strip()
            if not txt:
                continue
            matches = list(pattern.finditer(txt))
            if matches:
                for m in matches:
                    qty = float(m.group(1))
                    unit = m.group(2).lower().rstrip(".")
                    name = re.sub(r"\s+", " ", m.group(3)).strip(" .,-")
                    if name:
                        extracted.append(
                            {
                                "material_name": name,
                                "quantity": round(qty, 2),
                                "unit": unit,
                                "status": slots.get("material_status") or "needed",
                            }
                        )
                if extracted:
                    slots["material_lines_json"] = json.dumps(extracted, ensure_ascii=False)
                    break
    if not slots.get("material_status") and not slots.get("material_lines_json"):
        combined = " ".join((t or "").lower() for t in (user_text, heard_transcript, slots.get("summary_title")))
        if any(p in combined for p in ("in shop", "in ship", "arrived", "received")):
            slots["material_status"] = "in_shop"
        elif "ordered" in combined:
            slots["material_status"] = "ordered"
        elif "to order" in combined:
            slots["material_status"] = "needed"


_ADD_ITEM_TITLE_RE = re.compile(
    r"^(?:add|create|insert|new|include)\s+(?:furniture\s+item\s+|item\s+)?(.+?)\s+(?:to|on|in|for|under)\s+.+$",
    re.IGNORECASE,
)
_INLINE_DIM_RE = re.compile(
    r"(?<!\w)(\d+(?:\.\d+)?(?:\s*(?:ft|in|cm|mm|['\"]))?\s*[x×]\s*\d+(?:\.\d+)?(?:\s*(?:ft|in|cm|mm|['\"]))?)(?!\w)",
    re.IGNORECASE,
)


def parse_initial_items_slot(slots: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse structured `initial_items_json`, falling back to semicolon/plus `rough_scope` blocks on `new_lead`."""
    raw = slots.get("initial_items_json")
    parsed: list[dict[str, Any]] = []
    if raw:
        loaded: Any = raw
        if isinstance(raw, str):
            try:
                loaded = json.loads(raw)
            except (TypeError, ValueError):
                loaded = []
        if isinstance(loaded, list):
            for entry in loaded:
                if isinstance(entry, dict):
                    iname = str(entry.get("item_name") or entry.get("name") or "").strip()
                    if not iname:
                        continue
                    dims = str(entry.get("dimensions") or "").strip() or None
                    specs = str(entry.get("specs_notes") or entry.get("specs") or entry.get("notes") or "").strip() or None
                    parsed.append({"item_name": iname, "dimensions": dims, "specs_notes": specs})
                elif isinstance(entry, str) and entry.strip():
                    parsed.append({"item_name": entry.strip(), "dimensions": None, "specs_notes": None})

    if not parsed and str(slots.get("operation_kind") or "") == "new_lead":
        scope = str(slots.get("rough_scope") or "").strip()
        if ";" in scope and (":" in scope or "+" in scope):
            for block in (b.strip() for b in scope.split(";") if b.strip()):
                prefix = ""
                rest = block
                if ":" in block:
                    head, tail = block.split(":", 1)
                    if head.strip() and len(head.strip()) <= 32:
                        prefix, rest = head.strip(), tail.strip()
                parts = [p.strip() for p in rest.split("+") if p.strip()]
                for part in parts:
                    iname = f"{prefix} - {part}" if prefix else part
                    m_dim = _INLINE_DIM_RE.search(part)
                    parsed.append(
                        {
                            "item_name": iname,
                            "dimensions": m_dim.group(1).strip() if m_dim else None,
                            "specs_notes": None,
                        }
                    )
            if parsed and not slots.get("initial_items_json"):
                slots["initial_items_json"] = json.dumps(parsed, ensure_ascii=False)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in parsed:
        key = it["item_name"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(it)
    return deduped


def _backfill_item_slots_from_title_and_text(
    slots: dict[str, Any],
    *,
    summary_title: str | None,
    user_text: str | None,
    heard_transcript: str | None,
) -> None:
    """Rescue item_name and inline dimensions when LLM puts the furniture name only in summary_title."""
    if str(slots.get("operation_kind") or "") != "item_upsert_or_move":
        return
    if not slots.get("item_id") and not str(slots.get("item_name") or "").strip():
        for src in (summary_title, slots.get("summary_title")):
            t_str = (src or "").strip()
            if not t_str:
                continue
            m_title = _ADD_ITEM_TITLE_RE.match(t_str)
            if m_title:
                cand = re.sub(r"\s+", " ", m_title.group(1)).strip(" \t\r\n.,:-")
                if cand and cand.lower() not in _NULL_STRINGS:
                    slots["item_name"] = cand
                    break
    if not str(slots.get("dimensions") or "").strip():
        for src in (slots.get("item_name"), summary_title, user_text, heard_transcript):
            m_dim = _INLINE_DIM_RE.search(str(src or ""))
            if m_dim:
                slots["dimensions"] = m_dim.group(1).strip()
                break


def _backfill_onboarding_and_open_goal_slots(
    slots: dict[str, Any],
    *,
    summary_title: str | None,
    user_text: str | None,
    heard_transcript: str | None,
    prior_missing_slots: list[str] | None = None,
    is_ambiguous: bool = False,
) -> None:
    """Deterministically rescue area_landmark and single-slot open-goal answers when LLM puts them only in summary_title."""
    op = str(slots.get("operation_kind") or "")
    if op in ("new_lead", "add_customer", "update_customer", "update_order_details") and not str(
        slots.get("area_landmark") or ""
    ).strip():
        for raw_txt in (user_text, heard_transcript):
            txt = (raw_txt or "").strip()
            if not txt:
                continue
            m_explicit = re.search(
                r"\b(?:site\s+area(?:\s*/\s*landmark)?|area\s+landmark)\s*(?:is|=|:|-)?\s*([A-Za-z][A-Za-z0-9\s,.'/-]{1,48})",
                txt,
                flags=re.IGNORECASE,
            )
            if m_explicit:
                cand_area = re.sub(r"\s+", " ", m_explicit.group(1)).strip(" \t\r\n.,;!-")
                if cand_area:
                    slots["area_landmark"] = cand_area
                    break

    if op in ("new_lead", "add_customer", "update_customer", "update_order_details") and not str(
        slots.get("area_landmark") or ""
    ).strip():
        fname = str(slots.get("full_name") or "").strip()
        if fname:
            for title_src in (summary_title, slots.get("summary_title")):
                t_str = (title_src or "").strip()
                if not t_str:
                    continue
                m_paren = re.search(
                    rf"{re.escape(fname)}\s*\(([^()]+)\)",
                    t_str,
                    flags=re.IGNORECASE,
                )
                if m_paren:
                    cand = re.sub(r"\s+", " ", m_paren.group(1)).strip(" \t\r\n.,;!-")
                    c_low = cand.lower()
                    if (
                        2 <= len(cand) <= 48
                        and not c_low.startswith("prj-")
                        and "₹" not in cand
                        and c_low not in _NON_AREA_PARENS
                        and "material" not in c_low
                        and "execution" not in c_low
                    ):
                        slots["area_landmark"] = cand
                        break

    if (
        prior_missing_slots
        and len(prior_missing_slots) == 1
        and not is_ambiguous
        and prior_missing_slots[0] in ("area_landmark", "rough_scope", "full_name", "party_name", "item_name")
    ):
        only_slot = prior_missing_slots[0]
        if not str(slots.get(only_slot) or "").strip():
            raw_reply = (user_text or heard_transcript or "").strip()
            clean_reply = raw_reply.strip(" \t\r\n\"'.,!")
            if (
                clean_reply
                and 1 <= len(clean_reply) <= 120
                and not raw_reply.endswith("?")
                and not clean_reply.startswith("/")
                and clean_reply.lower() not in {"cancel", "discard", "stop", "nahi", "no", "wait", "later"}
            ):
                slots[only_slot] = clean_reply


def _compute_missing_slots_for_op(op: str, slots: dict[str, Any]) -> list[str]:
    """Compute required missing slots deterministically for a given operation kind."""
    if op in inventory_ai.INVENTORY_OPS:
        return inventory_ai.compute_inventory_missing_slots(op, slots)
    missing: list[str] = []
    if op == "new_lead":
        if not slots.get("customer_id"):
            missing = compute_missing_customer_onboarding_slots(slots)
        if float(slots.get("advance_amount_inr") or 0) > 0 and not slots.get("payment_account_id"):
            missing.append("payment_account_code")
    elif op == "add_customer":
        if not (slots.get("full_name") or slots.get("party_name")):
            missing.append("full_name")
    elif op == "add_person_or_vendor":
        if not (slots.get("full_name") or slots.get("party_name")):
            missing.append("full_name")
    elif op == "update_customer":
        if not slots.get("customer_id"):
            missing.append("customer_id")
    elif op == "update_vendor":
        if not slots.get("vendor_id"):
            missing.append("vendor_id")
    elif op == "update_person":
        if not slots.get("person_id"):
            missing.append("person_id")
    elif op == "update_order_details":
        if not slots.get("project_id"):
            missing.append("project_id")
    elif op == "archive_record":
        kind = slots.get("archive_entity_kind")
        if kind not in ("customer", "vendor", "person", "order"):
            missing.append("archive_entity_kind")
        else:
            id_map = {"customer": "customer_id", "vendor": "vendor_id", "person": "person_id", "order": "project_id"}
            req_id = id_map[kind]
            if not slots.get(req_id):
                missing.append(req_id)
    elif op in ("pipeline_stage_move", "material_stage_move"):
        if not slots.get("project_id"):
            missing.append("project_id")
        if not slots.get("to_stage"):
            missing.append("to_stage")
    elif op == "item_upsert_or_move":
        if not slots.get("project_id"):
            missing.append("project_id")
        if not slots.get("item_id") and not slots.get("item_name"):
            missing.append("item_name")
    elif op == "item_material_upsert":
        if not slots.get("project_id"):
            missing.append("project_id")
        elif not slots.get("material_lines_json") and not slots.get("material_status"):
            missing.append("material_lines_json")
    elif op in ("customer_receipt", "customer_payment", "vendor_payment", "expense", "float_transfer"):
        if not slots.get("amount_inr") or float(slots.get("amount_inr") or 0) <= 0:
            missing.append("amount_inr")
        if not slots.get("payment_account_id"):
            missing.append("payment_account_code")
        if op == "float_transfer" and not slots.get("to_payment_account_id"):
            missing.append("to_payment_account_code")
        if op in ("customer_receipt", "customer_payment", "vendor_payment") and not slots.get("party_name") and not slots.get("project_id"):
            missing.append("party_name")
    elif op in ("supplier_draft_bill", "customer_draft_invoice"):
        if not slots.get("party_name"):
            missing.append("party_name")
        if slots.get("amount_inr") is None:
            missing.append("amount_inr")
    return missing


def _format_quick_account_label(code: str, display_name: str, account_kind: str) -> str:
    c_low = code.strip().lower()
    raw_name = re.sub(r"\s*\([^)]*\)", "", display_name or "").strip()
    if account_kind == "cash_galla" or c_low == "cash_galla":
        icon = "💵"
        short = raw_name or "Cash Galla"
    elif account_kind == "supervisor_float" or c_low.startswith("float_"):
        icon = "👛"
        short = re.sub(r"\s+(?:wallet|float)$", "", raw_name, flags=re.IGNORECASE).strip() or raw_name or c_low
    else:
        icon = "🏦"
        short = raw_name or c_low.upper()
    return f"{icon} {short}"[:22].strip()


def _build_default_missing_slot_choices(
    sb: Any, missing_slot: str, op: str
) -> list[dict[str, Any]]:
    """Build 1-tap quick-pick choices for single-slot prompts with predictable workshop defaults."""
    if missing_slot == "area_landmark":
        return [
            {"choice_id": f"area_{city.lower()}", "label": f"📍 {city}", "slot_patch_json": json.dumps({"area_landmark": city})}
            for city in ("Beawar", "Jaipur", "Ajmer", "Chittorgarh")
        ]

    if missing_slot in ("payment_account_code", "to_payment_account_code"):
        acc_rows = sb.table("payment_accounts").select("code,display_name,account_kind,sort_order").eq("is_active", True).order("sort_order").execute().data or []
        pref_rank = {
            c: idx
            for idx, c in enumerate(
                ("cash_galla", "axis", "rgb", "float_rohit", "float_kamlesh", "ccrgb", "float_ansh")
            )
        }
        ordered = sorted(
            [a for a in acc_rows if a.get("code")],
            key=lambda a: (pref_rank.get(str(a["code"]).strip().lower(), len(pref_rank)), int(a.get("sort_order") or 999), str(a["code"])),
        )
        return [
            {
                "choice_id": f"acc_{a['code']}",
                "label": _format_quick_account_label(str(a["code"]), str(a.get("display_name") or a["code"]), str(a.get("account_kind") or "")),
                "slot_patch_json": json.dumps({missing_slot: str(a["code"])}),
            }
            for a in ordered[:4]
        ]

    if missing_slot == "to_stage" and op == "material_stage_move":
        return [
            {"choice_id": f"mat_{sc}", "label": lbl, "slot_patch_json": json.dumps({"to_stage": sc})}
            for sc, lbl in (("check_stock", "📦 Check Stock"), ("ordered_pending", "🚚 Ordered"), ("all_in_shop", "✅ All In Shop"))
        ]

    return []
