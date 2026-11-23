"""Read/query builders and standing SKU catalog upserts for /inventory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.goal_primitives import IST
from app.services.step_executors.inventory_matching import (
    match_inventory_sku,
    normalize_inventory_unit,
)
from app.services.step_executors.materials import infer_material_category


def _is_same_day_ist(created_at_raw: Any) -> bool:
    """Return True if an ISO timestamp falls on today's IST calendar date."""
    if not created_at_raw:
        return True
    try:
        dt = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).date() == datetime.now(IST).date()
    except ValueError:
        return False


def get_inventory_overview_data(sb: Any) -> dict[str, Any]:
    """Build complete data payload for /api/inventory/overview (flat tables, KPIs & To Buy)."""
    raw_items = sb.table("inventory_items").select("*").eq("is_active", True).order("name").execute().data or []
    categories = sb.table("material_categories").select("*").order("sort_order").execute().data or []
    cat_labels = {c["code"]: c["label"] for c in categories}

    enriched_items: list[dict[str, Any]] = []
    total_val = 0.0
    low_stock_skus: list[dict[str, Any]] = []
    for it in raw_items:
        qty = round(float(it.get("on_hand_qty") or 0), 2)
        min_q = round(float(it.get("min_stock_qty") or 0), 2)
        avg = round(float(it.get("avg_unit_cost_inr") or 0), 2)
        val = round(qty * avg, 2)
        is_low = qty <= min_q and min_q > 0
        total_val = round(total_val + val, 2)
        row = {
            **it,
            "on_hand_qty": qty,
            "min_stock_qty": min_q,
            "avg_unit_cost_inr": avg,
            "last_purchase_rate_inr": round(float(it.get("last_purchase_rate_inr") or 0), 2),
            "stock_value_inr": val,
            "is_low_stock": is_low,
            "category_label": cat_labels.get(it.get("category"), it.get("category") or "Ply / Wood"),
        }
        enriched_items.append(row)
        if is_low:
            low_stock_skus.append(row)

    active_orders = [
        p
        for p in (
            sb.table("projects")
            .select("id,project_code,customer_name,project_type,status")
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        if p.get("project_type") != "general_stock"
    ]
    orders_map = {o["id"]: o for o in active_orders}
    order_items = sb.table("project_items").select("id,project_id,item_name").execute().data or []
    item_names = {oi["id"]: oi["item_name"] for oi in order_items}

    all_mats = sb.table("item_materials").select("*").execute().data or []
    to_buy_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for m in all_mats:
        st = m.get("status")
        pid = m.get("project_id")
        if st not in ("needed", "ordered") or pid not in orders_map:
            continue
        unit = normalize_inventory_unit(m.get("unit"))
        m_name = str(m.get("material_name") or "").strip()
        key = (m.get("category") or "ply_wood", m_name.lower(), unit)
        sku = match_inventory_sku(sb, m.get("inventory_item_id") or m_name, items_cache=enriched_items)
        if key not in to_buy_map:
            to_buy_map[key] = {
                "key": f"{key[0]}::{key[1]}::{key[2]}",
                "material_name": m_name,
                "category": key[0],
                "category_label": cat_labels.get(key[0], key[0]),
                "unit": unit,
                "needed_qty": 0.0,
                "ordered_qty": 0.0,
                "total_demand_qty": 0.0,
                "in_stock_qty": sku["on_hand_qty"] if sku else 0.0,
                "matched_inventory_item_id": sku["id"] if sku else None,
                "matched_sku_name": sku["name"] if sku else None,
                "status": st,
                "source": "order",
                "material_ids": [],
                "order_lines": [],
            }
        entry = to_buy_map[key]
        qty_m = round(float(m.get("quantity") or 0), 2)
        entry["total_demand_qty"] = round(entry["total_demand_qty"] + qty_m, 2)
        if st == "needed":
            entry["needed_qty"] = round(entry["needed_qty"] + qty_m, 2)
        else:
            entry["ordered_qty"] = round(entry["ordered_qty"] + qty_m, 2)
        entry["status"] = "needed" if entry["needed_qty"] > 0 else "ordered"
        entry["material_ids"].append(m["id"])
        ord_obj = orders_map[pid]
        entry["order_lines"].append({
            "material_id": m["id"],
            "project_id": pid,
            "project_code": ord_obj.get("project_code"),
            "customer_name": ord_obj.get("customer_name"),
            "item_id": m.get("item_id"),
            "item_name": item_names.get(m.get("item_id"), "Order-Wide"),
            "quantity": qty_m,
            "status": st,
        })

    matched_sku_ids_in_orders = {
        v["matched_inventory_item_id"] for v in to_buy_map.values() if v.get("matched_inventory_item_id")
    }
    to_buy_rows = list(to_buy_map.values())
    for sku in low_stock_skus:
        shortfall = round(max(sku["min_stock_qty"] - sku["on_hand_qty"], 1.0), 2)
        if sku["id"] in matched_sku_ids_in_orders:
            for row in to_buy_rows:
                if row.get("matched_inventory_item_id") == sku["id"]:
                    row["is_low_stock_sku"] = True
            continue
        to_buy_rows.append({
            "key": f"sku::{sku['id']}",
            "material_name": sku["name"],
            "category": sku["category"],
            "category_label": sku["category_label"],
            "unit": sku["unit"],
            "needed_qty": shortfall,
            "ordered_qty": 0.0,
            "total_demand_qty": shortfall,
            "in_stock_qty": sku["on_hand_qty"],
            "matched_inventory_item_id": sku["id"],
            "matched_sku_name": sku["name"],
            "status": "low_stock",
            "is_low_stock_sku": True,
            "source": "workshop_restock",
            "material_ids": [],
            "order_lines": [],
        })

    vendors = {
        v["id"]: v
        for v in (sb.table("vendors").select("id,canonical_name,vendor_kind").eq("is_active", True).execute().data or [])
    }
    accounts = {
        a["id"]: a
        for a in (
            sb.table("payment_accounts").select("id,code,display_name,account_kind").eq("is_active", True).execute().data or []
        )
    }
    all_orders_map = {
        p["id"]: p for p in (sb.table("projects").select("id,project_code,customer_name").execute().data or [])
    }

    raw_movs = sb.table("inventory_movements").select("*").order("created_at", desc=True).limit(120).execute().data or []
    movements: list[dict[str, Any]] = []
    rate_pending_count = 0
    for mv in raw_movs:
        is_rp = mv.get("zoho_sync_status") == "rate_pending" and not mv.get("is_undone")
        if is_rp:
            rate_pending_count += 1
        prj = all_orders_map.get(mv.get("project_id"))
        vend = vendors.get(mv.get("vendor_id"))
        acc = accounts.get(mv.get("payment_account_id"))
        movements.append({
            **mv,
            "category_label": cat_labels.get(mv.get("category"), mv.get("category") or "Ply / Wood"),
            "project_code": prj.get("project_code") if prj else None,
            "customer_name": prj.get("customer_name") if prj else None,
            "vendor_name": vend.get("canonical_name") if vend else None,
            "account_name": acc.get("display_name") if acc else None,
            "can_undo": (not mv.get("is_undone")) and _is_same_day_ist(mv.get("created_at")),
        })

    return {
        "items": enriched_items,
        "to_buy": to_buy_rows,
        "movements": movements,
        "kpis": {
            "total_stock_value_inr": total_val,
            "sku_count": len(enriched_items),
            "low_stock_count": len(low_stock_skus),
            "rate_pending_count": rate_pending_count,
        },
        "categories": categories,
        "active_orders": [
            {**o, "items": [oi for oi in order_items if oi.get("project_id") == o["id"]]}
            for o in active_orders
        ],
        "vendors": [v for v in vendors.values() if v.get("vendor_kind") == "supplier"],
        "payment_accounts": list(accounts.values()),
    }


def upsert_inventory_item(
    sb: Any,
    *,
    sku_id: str | None = None,
    name: str,
    category: str | None = None,
    unit: str = "pcs",
    min_stock_qty: float = 0.0,
    avg_unit_cost_inr: float | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    """Create or update a standing Workshop Stock SKU."""
    clean_name = (name or "").strip()
    if not clean_name and not sku_id:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "SKU name is required."}}

    clean_unit = normalize_inventory_unit(unit)
    clean_cat = (
        category
        if category in ("ply_wood", "laminate", "hardware", "foam_fabric", "polish")
        else infer_material_category(clean_name)
    )
    clean_aliases = [str(a).strip() for a in (aliases or []) if str(a).strip()]

    if sku_id:
        cur = sb.table("inventory_items").select("*").eq("id", sku_id).limit(1).execute().data or []
        if not cur:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Inventory SKU not found."}}
        patch: dict[str, Any] = {
            **({"name": clean_name} if clean_name else {}),
            **({"category": clean_cat} if category else {}),
            **({"unit": clean_unit} if unit else {}),
            "min_stock_qty": round(max(float(min_stock_qty or 0), 0.0), 2),
            **({"aliases": clean_aliases} if aliases is not None else {}),
            **({"notes": notes.strip() or None} if notes is not None else {}),
            **({"is_active": is_active} if is_active is not None else {}),
        }
        if avg_unit_cost_inr is not None and float(avg_unit_cost_inr) >= 0:
            rate = round(float(avg_unit_cost_inr), 2)
            patch["avg_unit_cost_inr"] = rate
            patch["last_purchase_rate_inr"] = rate
        upd = sb.table("inventory_items").update(patch).eq("id", sku_id).execute().data or [cur[0]]
        return {"ok": True, "item": upd[0], "receipt": f"📦 Updated *{upd[0]['name']}*"}

    dup = match_inventory_sku(sb, clean_name)
    if dup and dup.get("name", "").strip().lower() == clean_name.lower():
        return {"ok": True, "item": dup, "deduped": True, "receipt": f"📦 Using existing SKU *{dup['name']}*"}

    rate_val = round(max(float(avg_unit_cost_inr or 0), 0.0), 2)
    ins = sb.table("inventory_items").insert({
        "name": clean_name,
        "category": clean_cat,
        "unit": clean_unit,
        "aliases": clean_aliases,
        "on_hand_qty": 0.0,
        "min_stock_qty": round(max(float(min_stock_qty or 0), 0.0), 2),
        "avg_unit_cost_inr": rate_val,
        "last_purchase_rate_inr": rate_val,
        "is_active": True,
        "notes": (notes or "").strip() or None,
    }).execute().data or []
    return {"ok": True, "item": ins[0], "receipt": f"📦 Added SKU *{ins[0]['name']}*"}
