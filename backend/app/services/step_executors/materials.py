"""Item-level raw material CRUD and order Material Track synchronization."""

from __future__ import annotations

from typing import Any

from app.services.goal_primitives import build_stage_undo_snapshot
from app.services.step_executors.common import get_default_person_id, parse_positive_quantity
from app.services.step_executors.production import upsert_or_transition_item

MATERIAL_STATUS_LABELS: dict[str, str] = {
    "needed": "To Order",
    "ordered": "Ordered",
    "in_shop": "In Shop",
}

_VALID_MATERIAL_CATEGORIES = {"ply_wood", "laminate", "hardware", "foam_fabric", "polish"}


def normalize_material_status(raw: str | None) -> str:
    """Normalize user/AI status text into DB CHECK values ('needed' | 'ordered' | 'in_shop')."""
    val = (raw or "needed").strip().lower().replace("-", "_").replace(" ", "_")
    if val in ("in_shop", "in_ship", "inshop", "received", "arrived", "shop"):
        return "in_shop"
    if val in ("ordered", "order_placed", "booked"):
        return "ordered"
    if val in ("needed", "to_order", "order", "pending", "required"):
        return "needed"
    return "needed"


def infer_material_category(material_name: str, explicit_category: str | None = None) -> str:
    """Auto-classify a raw material into one of the 5 material_categories rows."""
    if explicit_category and explicit_category.strip().lower() in _VALID_MATERIAL_CATEGORIES:
        return explicit_category.strip().lower()
    txt = (material_name or "").strip().lower()
    if any(
        kw in txt
        for kw in (
            "laminate",
            "mica",
            "sunmica",
            "acrylic",
            "louver",
            "fluted",
            "charcoal",
            "veneer",
            "edgeband",
            "edge band",
            "pvc",
            "plastic strip",
            "strip",
            "highlighter",
        )
    ):
        return "laminate"
    if any(
        kw in txt
        for kw in (
            "hinge",
            "kabja",
            "kabje",
            "channel",
            "tandem",
            "telescopic",
            "handle",
            "knob",
            "lock",
            "screw",
            "minifix",
            "profile",
            "led",
            "driver",
            "light",
            "glass",
            "mirror",
            "basket",
            "bpo",
            "gtpt",
            "gt-pt",
            "magic corner",
            "leg",
            "hydraulic",
            "slido",
            "hardware",
        )
    ):
        return "hardware"
    if any(
        kw in txt
        for kw in (
            "foam",
            "sleepwell",
            "feather",
            "fabric",
            "cloth",
            "rexine",
            "leatherette",
            "curtain",
            "blind",
            "track",
            "cushion",
            "upholstery",
            "mattress",
            "gadda",
            "fibre",
            "fiber",
        )
    ):
        return "foam_fabric"
    if any(
        kw in txt
        for kw in (
            "fevicol",
            "adhesive",
            "glue",
            "heatx",
            "swr",
            "marine",
            "polish",
            "melamine",
            "deco",
            "paint",
            "primer",
            "thinner",
            "sandpaper",
            "regmal",
            "tape",
            "masking",
            "pu ",
        )
    ):
        return "polish"
    return "ply_wood"


def sync_order_material_track_from_items(
    sb: Any,
    *,
    project_id: str,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> list[str]:
    """Derive projects.material_stage from item_materials rows when any materials exist."""
    rows = (
        sb.table("item_materials")
        .select("id,status")
        .eq("project_id", project_id)
        .execute()
        .data
        or []
    )
    if not rows:
        return []

    statuses = {str(r.get("status") or "needed") for r in rows}
    if statuses == {"in_shop"}:
        desired_stage = "all_in_shop"
    elif "ordered" in statuses or "in_shop" in statuses:
        desired_stage = "ordered_pending"
    else:
        desired_stage = "check_stock"

    prj_rows = sb.table("projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not prj_rows:
        return []
    prj = prj_rows[0]
    if prj.get("material_stage") == desired_stage:
        return []

    prior_snap = build_stage_undo_snapshot(track="material", row_before=prj)
    owner_id = prj.get("material_owner_id") or get_default_person_id(sb, "is_default_purchase")
    patch: dict[str, Any] = {"material_stage": desired_stage}
    if owner_id:
        patch["material_owner_id"] = owner_id
    sb.table("projects").update(patch).eq("id", project_id).execute()

    src = "telegram" if source_channel == "telegram" else "web"
    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "track": "material",
                "from_stage": prj.get("material_stage"),
                "to_stage": desired_stage,
                "prior_snapshot": prior_snap,
                "assigned_people_ids": [owner_id] if owner_id else [],
                "actor_person_id": actor_person_id,
                "source_channel": src,
                "note": "Auto-synced from item materials",
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    return [str(ev[0]["id"])] if ev else []


async def add_item_materials_batch(
    sb: Any,
    *,
    project_id: str,
    item_id: str | None = None,
    item_name: str | None = None,
    materials: list[dict[str, Any]],
    actor_person_id: str | None = None,
    source_channel: str = "web",
    heard_transcript: str | None = None,
    allow_order_wide: bool = False,
    inventory_item_id: str | None = None,
) -> dict[str, Any]:
    """Add one or more raw material lines under a furniture item (or order-wide) and auto-sync Material Track."""
    prj_rows = (
        sb.table("projects")
        .select("id,project_code,customer_name")
        .eq("id", project_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not prj_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Order not found."}}
    prj = prj_rows[0]

    src = "telegram" if source_channel == "telegram" else "web"
    undo_ids: list[str] = []
    target_item: dict[str, Any] | None = None

    if item_id:
        found = sb.table("project_items").select("*").eq("id", item_id).limit(1).execute().data or []
        if not found:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Furniture item not found."}}
        target_item = found[0]
    elif item_name and item_name.strip():
        found = (
            sb.table("project_items")
            .select("*")
            .eq("project_id", project_id)
            .ilike("item_name", item_name.strip())
            .limit(1)
            .execute()
            .data
            or []
        )
        if found:
            target_item = found[0]
        else:
            created_res = await upsert_or_transition_item(
                sb,
                project_id=project_id,
                item_name=item_name.strip(),
                to_stage="in_shop",
                actor_person_id=actor_person_id,
                source_channel=src,
                heard_transcript=heard_transcript,
            )
            if not created_res.get("ok"):
                return created_res
            undo_ids.extend(created_res.get("undo_event_ids") or [])
            target_item = (
                sb.table("project_items")
                .select("*")
                .eq("id", created_res["item_id"])
                .limit(1)
                .execute()
                .data
                or []
            )[0]
    elif not allow_order_wide:
        order_items = (
            sb.table("project_items")
            .select("*")
            .eq("project_id", project_id)
            .execute()
            .data
            or []
        )
        if len(order_items) == 1:
            target_item = order_items[0]
        else:
            return {
                "ok": False,
                "error_kind": "validation_400",
                "error": {"message": "Please specify which furniture item this material belongs to."},
            }

    target_item_id = target_item["id"] if target_item else None
    target_item_label = target_item["item_name"] if target_item else "Order-Wide"

    inserted_rows: list[dict[str, Any]] = []
    for raw_line in materials or []:
        m_name = str(raw_line.get("material_name") or "").strip()
        if not m_name:
            continue
        qty_val, parsed_unit = parse_positive_quantity(raw_line.get("quantity"), default=1.0)
        raw_unit = str(raw_line.get("unit") or "").strip()
        unit_val = raw_unit or parsed_unit or "pcs"
        cat_val = infer_material_category(m_name, raw_line.get("category"))
        st_val = normalize_material_status(raw_line.get("status"))
        inv_id = raw_line.get("inventory_item_id") or inventory_item_id
        payload: dict[str, Any] = {
            "project_id": project_id,
            "item_id": target_item_id,
            "category": cat_val,
            "material_name": m_name,
            "quantity": qty_val,
            "unit": unit_val,
            "status": st_val,
            "notes": (str(raw_line.get("notes") or "").strip() or None),
            **({"inventory_item_id": inv_id} if inv_id else {}),
        }
        ins = sb.table("item_materials").insert(payload).execute().data or []
        if ins:
            inserted_rows.append(ins[0])

    if not inserted_rows:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": "At least one material name is required."},
        }

    created_ids = [str(r["id"]) for r in inserted_rows]
    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "item_id": target_item_id,
                "track": "item",
                "from_stage": None,
                "to_stage": "material_added",
                "prior_snapshot": {"_item_material_action": "created", "material_ids": created_ids},
                "actor_person_id": actor_person_id,
                "source_channel": src,
                "heard_transcript": heard_transcript,
                "note": ", ".join(r["material_name"] for r in inserted_rows)[:220],
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    if ev:
        undo_ids.append(str(ev[0]["id"]))

    undo_ids.extend(
        sync_order_material_track_from_items(
            sb,
            project_id=project_id,
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    )

    if len(inserted_rows) == 1:
        r0 = inserted_rows[0]
        q_fmt = int(r0["quantity"]) if float(r0["quantity"]).is_integer() else r0["quantity"]
        desc = f"*{r0['material_name']}* ({q_fmt} {r0['unit']})"
    else:
        desc = f"*{len(inserted_rows)} materials*"

    return {
        "ok": True,
        "project_id": project_id,
        "item_id": target_item_id,
        "material_ids": created_ids,
        "materials": inserted_rows,
        "undo_event_ids": undo_ids,
        "receipt": f"🪵 Added {desc} → *{target_item_label}* (`{prj['customer_name']}`)",
    }


def update_item_materials_rows(
    sb: Any,
    *,
    material_ids: list[str],
    patch: dict[str, Any],
    actor_person_id: str | None = None,
    source_channel: str = "web",
    heard_transcript: str | None = None,
) -> dict[str, Any]:
    """Update status/qty/fields on one or more item_materials rows (e.g. customer-level bulk update)."""
    ids = [str(mid) for mid in (material_ids or []) if mid]
    if not ids:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "No material IDs provided."}}

    existing = sb.table("item_materials").select("*").in_("id", ids).execute().data or []
    if not existing:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Raw material line not found."}}

    before_rows = [
        {
            "id": r["id"],
            "category": r.get("category"),
            "material_name": r.get("material_name"),
            "quantity": r.get("quantity"),
            "unit": r.get("unit"),
            "status": r.get("status"),
            "notes": r.get("notes"),
        }
        for r in existing
    ]

    clean_patch: dict[str, Any] = {}
    if patch.get("status") is not None:
        clean_patch["status"] = normalize_material_status(patch["status"])
    if patch.get("material_name") is not None and str(patch["material_name"]).strip():
        clean_patch["material_name"] = str(patch["material_name"]).strip()
        if not patch.get("category"):
            clean_patch["category"] = infer_material_category(clean_patch["material_name"])
    if patch.get("category") and str(patch["category"]).strip().lower() in _VALID_MATERIAL_CATEGORIES:
        clean_patch["category"] = str(patch["category"]).strip().lower()
    if patch.get("quantity") is not None:
        qty_val, parsed_unit = parse_positive_quantity(patch["quantity"], default=1.0)
        clean_patch["quantity"] = qty_val
        if not patch.get("unit") and parsed_unit:
            clean_patch["unit"] = parsed_unit
    if patch.get("unit") is not None and str(patch["unit"]).strip():
        clean_patch["unit"] = str(patch["unit"]).strip()
    if "notes" in patch and patch["notes"] is not None:
        clean_patch["notes"] = str(patch["notes"]).strip() or None

    if not clean_patch:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "No material fields to update."}}

    for r in existing:
        sb.table("item_materials").update(clean_patch).eq("id", r["id"]).execute()

    first_row = existing[0]
    project_id = str(first_row["project_id"])
    src = "telegram" if source_channel == "telegram" else "web"
    undo_ids: list[str] = []

    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "item_id": first_row["item_id"],
                "track": "item",
                "from_stage": first_row.get("status"),
                "to_stage": clean_patch.get("status") or "material_updated",
                "prior_snapshot": {"_item_material_action": "updated", "before_rows": before_rows},
                "actor_person_id": actor_person_id,
                "source_channel": src,
                "heard_transcript": heard_transcript,
                "note": f"Updated {len(existing)} material line(s)",
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    if ev:
        undo_ids.append(str(ev[0]["id"]))

    undo_ids.extend(
        sync_order_material_track_from_items(
            sb,
            project_id=project_id,
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    )

    label_status = MATERIAL_STATUS_LABELS.get(clean_patch.get("status", ""), "")
    mat_label = clean_patch.get("material_name") or first_row.get("material_name") or "Material"
    receipt = (
        f"🪵 *{mat_label}* ({len(existing)} line{'s' if len(existing) != 1 else ''}) → *{label_status}*"
        if label_status
        else f"🪵 Updated *{mat_label}*"
    )
    return {
        "ok": True,
        "project_id": project_id,
        "material_ids": [str(r["id"]) for r in existing],
        "undo_event_ids": undo_ids,
        "receipt": receipt,
    }


def delete_item_material_row(
    sb: Any,
    *,
    material_id: str,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Delete an item_materials row with reversible snapshot + track re-sync."""
    rows = sb.table("item_materials").select("*").eq("id", material_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Raw material line not found."}}
    cur = rows[0]
    snapshot_row = {
        "id": cur["id"],
        "project_id": cur["project_id"],
        "item_id": cur["item_id"],
        "category": cur.get("category") or "ply_wood",
        "material_name": cur["material_name"],
        "quantity": cur.get("quantity", 1),
        "unit": cur.get("unit") or "pcs",
        "status": cur.get("status") or "needed",
        "preferred_supplier": cur.get("preferred_supplier"),
        "notes": cur.get("notes"),
    }
    sb.table("item_materials").delete().eq("id", material_id).execute()

    src = "telegram" if source_channel == "telegram" else "web"
    undo_ids: list[str] = []
    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": cur["project_id"],
                "item_id": cur["item_id"],
                "track": "item",
                "from_stage": cur.get("status"),
                "to_stage": "material_deleted",
                "prior_snapshot": {"_item_material_action": "deleted", "deleted_row": snapshot_row},
                "actor_person_id": actor_person_id,
                "source_channel": src,
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    if ev:
        undo_ids.append(str(ev[0]["id"]))

    undo_ids.extend(
        sync_order_material_track_from_items(
            sb,
            project_id=str(cur["project_id"]),
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    )
    return {
        "ok": True,
        "project_id": cur["project_id"],
        "undo_event_ids": undo_ids,
        "receipt": f"Removed *{cur['material_name']}*",
    }
