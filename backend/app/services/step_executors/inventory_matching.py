"""Deterministic Hinglish/trade synonym normalization, SKU matching, and order material fulfillment."""

from __future__ import annotations

import re
from typing import Any

from app.services.step_executors.materials import (
    infer_material_category,
    sync_order_material_track_from_items,
)

_UNIT_CANON: dict[str, str] = {
    "sht": "sheets",
    "shts": "sheets",
    "sheet": "sheets",
    "sheets": "sheets",
    "pc": "pcs",
    "pcs": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
    "no": "pcs",
    "nos": "pcs",
    "mtr": "mtr",
    "mtrs": "mtr",
    "meter": "mtr",
    "meters": "mtr",
    "sqft": "sqft",
    "rft": "rft",
    "kg": "kg",
    "kgs": "kg",
    "ltr": "ltr",
    "ltrs": "ltr",
    "liter": "ltr",
    "liters": "ltr",
    "litre": "ltr",
    "litres": "ltr",
    "box": "box",
    "boxes": "box",
    "pkt": "box",
    "set": "set",
    "sets": "set",
    "pair": "set",
    "pairs": "set",
    "roll": "roll",
    "rolls": "roll",
}

_WORD_SYNONYMS: dict[str, str] = {
    "mica": "laminate",
    "sunmica": "laminate",
    "lam": "laminate",
    "laminates": "laminate",
    "kabja": "hinge",
    "kabje": "hinge",
    "kabza": "hinge",
    "hinges": "hinge",
    "sensys": "hinge",
    "louvers": "louver",
    "fluted": "louver",
    "charcoal": "louver",
    "panel": "louver",
    "gadda": "foam",
    "mattress": "foam",
    "fevicol": "adhesive",
    "glue": "adhesive",
    "regmal": "sandpaper",
    "plywood": "ply",
    "board": "ply",
    "channels": "channel",
    "slider": "channel",
    "telescopic": "channel",
    "screws": "screw",
    "pech": "screw",
    "handles": "handle",
    "locks": "lock",
}

_NOISE_WORDS = frozenset({"mm", "and", "the", "for", "of", "with", "in", "size", "type", "grade"})


def normalize_inventory_unit(raw_unit: str | None) -> str:
    txt = re.sub(r"[^a-z0-9]", "", (raw_unit or "").strip().lower())
    return _UNIT_CANON.get(txt, (raw_unit or "pcs").strip().lower() or "pcs")


def _normalized_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    tokens: set[str] = set()
    for raw in cleaned.split():
        if not raw or raw in _NOISE_WORDS or raw in _UNIT_CANON:
            continue
        mapped = _WORD_SYNONYMS.get(raw, raw)
        tokens.add(mapped)
    return tokens


def match_inventory_sku(
    sb: Any, name_or_id: str | None, items_cache: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Resolve an inventory_items row deterministically by ID, exact name, alias, or normalized tokens."""
    needle = (name_or_id or "").strip()
    if not needle:
        return None

    items = items_cache
    if items is None:
        items = (
            sb.table("inventory_items")
            .select("*")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )

    low = needle.lower()
    for it in items:
        if str(it.get("id") or "") == needle:
            return it

    for it in items:
        names = [str(it.get("name") or "").strip().lower(), *(str(a).strip().lower() for a in (it.get("aliases") or []))]
        if low in names:
            return it

    n_toks = _normalized_tokens(needle)
    if not n_toks:
        return None

    best_score = 0.0
    best_item: dict[str, Any] | None = None
    for it in items:
        cand_texts = [str(it.get("name") or ""), *(str(a) for a in (it.get("aliases") or []))]
        for ct in cand_texts:
            c_toks = _normalized_tokens(ct)
            if not c_toks:
                continue
            if n_toks == c_toks:
                return it
            inter = len(n_toks & c_toks)
            if inter == 0:
                continue
            score = inter / max(len(n_toks), len(c_toks))
            if score > best_score:
                best_score = score
                best_item = it

    return best_item if best_score >= 0.65 else None


def match_order_open_material(
    sb: Any,
    *,
    project_id: str,
    incoming_name: str,
    incoming_unit: str | None = None,
    inventory_item_id: str | None = None,
    open_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Instant (<1ms) local Layer 1 matching against open `needed`/`ordered` rows on one Customer Order."""
    rows = open_rows
    if rows is None:
        all_rows = (
            sb.table("item_materials")
            .select("*")
            .eq("project_id", project_id)
            .execute()
            .data
            or []
        )
        rows = [r for r in all_rows if r.get("status") in ("needed", "ordered")]

    if not rows:
        return None

    if inventory_item_id:
        for r in rows:
            if r.get("inventory_item_id") == inventory_item_id:
                return r

    inc_low = (incoming_name or "").strip().lower()
    inc_unit = normalize_inventory_unit(incoming_unit) if incoming_unit else ""
    inc_cat = infer_material_category(incoming_name)
    inc_toks = _normalized_tokens(incoming_name)

    for r in rows:
        if str(r.get("material_name") or "").strip().lower() == inc_low:
            return r

    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        r_name = str(r.get("material_name") or "")
        r_toks = _normalized_tokens(r_name)
        r_cat = r.get("category") or infer_material_category(r_name)
        r_unit = normalize_inventory_unit(r.get("unit"))

        score = 0.0
        inter = len(inc_toks & r_toks)
        if inc_toks and r_toks and inter > 0:
            score += (inter / len(inc_toks | r_toks)) * 0.70
            if inc_toks <= r_toks or r_toks <= inc_toks:
                score += 0.15
        if r_cat == inc_cat:
            score += 0.25
        if inc_unit and r_unit == inc_unit:
            score += 0.10
        elif inc_unit and r_unit != inc_unit:
            score -= 0.15

        if score >= 0.45:
            scored.append((score, r))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    # Require clear category+token fit (or unique category match when incoming mentions trade synonym)
    top_score, top_row = scored[0]
    if top_score >= 0.55:
        return top_row
    if len(scored) == 1 and top_score >= 0.45 and (inc_toks & _normalized_tokens(str(top_row.get("material_name") or ""))):
        return top_row
    return None


def apply_order_material_fulfillment(
    sb: Any,
    *,
    project_id: str,
    incoming_name: str,
    received_qty: float,
    unit: str,
    category: str | None = None,
    matched_item_material_id: str | None = None,
    item_id: str | None = None,
    inventory_item_id: str | None = None,
    notes: str | None = None,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Layer 3: Update/split a matched open row (preserving friendly name) OR auto-add unlisted custom row."""
    clean_name = (incoming_name or "").strip() or "Workshop Material"
    clean_unit = normalize_inventory_unit(unit)
    cat = category or infer_material_category(clean_name)
    recv = round(float(received_qty or 0), 2)

    target_row: dict[str, Any] | None = None
    if matched_item_material_id and matched_item_material_id != "new_unlisted":
        found = sb.table("item_materials").select("*").eq("id", matched_item_material_id).limit(1).execute().data or []
        if found and str(found[0].get("project_id")) == str(project_id):
            target_row = found[0]
    elif matched_item_material_id != "new_unlisted":
        target_row = match_order_open_material(
            sb,
            project_id=project_id,
            incoming_name=clean_name,
            incoming_unit=clean_unit,
            inventory_item_id=inventory_item_id,
        )

    undo_meta: dict[str, Any] = {"project_id": project_id}
    if target_row and target_row.get("status") in ("needed", "ordered"):
        undo_meta["before_row"] = dict(target_row)
        old_qty = round(float(target_row.get("quantity") or 0), 2)
        existing_notes = str(target_row.get("notes") or "").strip()
        note_parts = [existing_notes] if existing_notes else []
        if clean_name.lower() not in str(target_row.get("material_name") or "").lower() and clean_name.lower() not in existing_notes.lower():
            note_parts.append(f"Spec: {clean_name}")
        if notes and notes.strip() and notes.strip().lower() not in existing_notes.lower():
            note_parts.append(notes.strip())
        merged_notes = " | ".join(note_parts) or None

        patch: dict[str, Any] = {"status": "in_shop", "notes": merged_notes}
        if inventory_item_id and not target_row.get("inventory_item_id"):
            patch["inventory_item_id"] = inventory_item_id

        if 0 < recv < old_qty:
            patch["quantity"] = recv
            sb.table("item_materials").update(patch).eq("id", target_row["id"]).execute()
            rem_qty = round(old_qty - recv, 2)
            rem_payload = {
                "project_id": project_id,
                "item_id": target_row.get("item_id"),
                "category": target_row.get("category") or cat,
                "material_name": target_row["material_name"],
                "quantity": rem_qty,
                "unit": target_row.get("unit") or clean_unit,
                "status": target_row["status"],
                "preferred_supplier": target_row.get("preferred_supplier"),
                "notes": target_row.get("notes"),
                **({"inventory_item_id": target_row.get("inventory_item_id") or inventory_item_id} if (target_row.get("inventory_item_id") or inventory_item_id) else {}),
            }
            rem_ins = sb.table("item_materials").insert(rem_payload).execute().data or []
            if rem_ins:
                undo_meta["created_remainder_id"] = rem_ins[0]["id"]
        else:
            if recv > old_qty:
                patch["quantity"] = recv
            sb.table("item_materials").update(patch).eq("id", target_row["id"]).execute()

        result_material_id = target_row["id"]
        material_label = target_row["material_name"]
    else:
        payload: dict[str, Any] = {
            "project_id": project_id,
            "item_id": item_id or None,
            "category": cat,
            "material_name": clean_name,
            "quantity": recv if recv > 0 else 1.0,
            "unit": clean_unit,
            "status": "in_shop",
            "notes": (notes or "").strip() or None,
            **({"inventory_item_id": inventory_item_id} if inventory_item_id else {}),
        }
        ins = sb.table("item_materials").insert(payload).execute().data or []
        result_material_id = ins[0]["id"] if ins else None
        if result_material_id:
            undo_meta["created_new_id"] = result_material_id
        material_label = clean_name

    sync_events = sync_order_material_track_from_items(
        sb, project_id=project_id, actor_person_id=actor_person_id, source_channel=source_channel
    )
    undo_meta["material_track_event_ids"] = sync_events
    return {
        "item_material_id": result_material_id,
        "material_label": material_label,
        "undo_meta": undo_meta,
    }
