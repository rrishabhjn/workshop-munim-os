"""Inventory catalog, Moving Weighted Average math, Gate-In, Customer Attribution, Count Adjust & Undo."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import uuid

from app.services import zoho
from app.services.goal_primitives import format_inr, make_zoho_reference_number
from app.services.step_executors.common import parse_positive_quantity, sanitize_iso_date
from app.services.step_executors.inventory_matching import (
    apply_order_material_fulfillment,
    match_inventory_sku,
    normalize_inventory_unit,
)
from app.services.step_executors.inventory_queries import (
    _is_same_day_ist,
    get_inventory_overview_data,
    upsert_inventory_item,
)
from app.services.step_executors.materials import infer_material_category
from app.services.step_executors.production import undo_stage_events

_TWO_PLACES = Decimal("0.01")


def _q2(val: Any) -> Decimal:
    return Decimal(str(val or 0)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_weighted_avg_load(
    old_qty: float, old_avg: float, add_qty: float, unit_rate: float
) -> tuple[float, float, float]:
    """Compute (new_qty, new_avg_unit_cost, line_total) using Moving Weighted Average."""
    oq = _q2(old_qty)
    oa = _q2(old_avg)
    aq = _q2(add_qty)
    ur = _q2(unit_rate)
    line_total = (aq * ur).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    new_qty = (oq + aq).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if new_qty <= Decimal("0.00"):
        return 0.0, float(ur), float(line_total)
    new_val = (oq * oa) + line_total
    new_avg = (new_val / new_qty).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return float(new_qty), float(new_avg), float(line_total)


def compute_issue_cost_and_remaining(
    old_qty: float, old_avg: float, issue_qty: float
) -> tuple[float, float, float]:
    """Compute (new_qty, new_avg, issue_cost) ensuring zero stock strictly equals ₹0.00 value."""
    oq = _q2(old_qty)
    oa = _q2(old_avg)
    iq = _q2(issue_qty)
    new_qty = max(oq - iq, Decimal("0.00")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    total_before = (oq * oa).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if new_qty == Decimal("0.00"):
        return 0.0, 0.0, float(total_before)
    issue_cost = (iq * oa).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return float(new_qty), float(oa), float(issue_cost)


def _clean_opt_id(val: Any) -> str | None:
    s = str(val or "").strip()
    if not s or s.lower() in ("null", "none", "undefined", "new_unlisted"):
        return None
    return s


def _find_existing_by_ref(sb: Any, reference_number: str | None) -> list[dict[str, Any]]:
    if not reference_number:
        return []
    rows = (
        sb.table("inventory_movements")
        .select("*")
        .eq("zoho_reference_number", reference_number)
        .limit(5)
        .execute()
        .data
        or []
    )
    return [r for r in rows if not r.get("is_undone")]


def _adjust_project_cost_cache(sb: Any, project_id: str | None, delta_inr: float) -> float:
    if not project_id or abs(float(delta_inr or 0)) < 0.005:
        return 0.0
    rows = sb.table("projects").select("id,cost_cache_inr").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        return 0.0
    old_val = float(rows[0].get("cost_cache_inr") or 0)
    new_val = round(max(old_val + float(delta_inr), 0.0), 2)
    sb.table("projects").update({"cost_cache_inr": new_val}).eq("id", project_id).execute()
    return round(new_val - old_val, 2)


async def execute_inventory_load(
    sb: Any,
    *,
    settlement_mode: str,
    lines: list[dict[str, Any]],
    payment_account_id: str | None = None,
    vendor_id: str | None = None,
    vendor_name: str | None = None,
    vendor_phone: str | None = None,
    vendor_gstin: str | None = None,
    invoice_no: str | None = None,
    txn_date: str | None = None,
    notes: str | None = None,
    reference_number: str | None = None,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Load stock and/or direct custom customer lines in one batch (with Zoho sync or Rate Pending)."""
    mode = (settlement_mode or "manual").strip().lower()
    if mode not in ("paid_now", "vendor_udhaar", "manual", "rate_pending"):
        return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Invalid settlement_mode '{mode}'."}}
    if not lines:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "At least one material line is required."}}

    existing_movs = _find_existing_by_ref(sb, reference_number)
    if existing_movs:
        return {
            "ok": True,
            "deduped": True,
            "movements": existing_movs,
            "zoho_id": existing_movs[0].get("zoho_txn_id"),
            "zoho_type": existing_movs[0].get("zoho_txn_type"),
            "zoho_sync_status": existing_movs[0].get("zoho_sync_status"),
            "reference_number": reference_number,
            "receipt": "Already recorded.",
        }

    payment_account_id = _clean_opt_id(payment_account_id)
    vendor_id = _clean_opt_id(vendor_id)
    actor_person_id = _clean_opt_id(actor_person_id)

    resolved_vendor_name = (vendor_name or "").strip()
    vendor_zoho_id = None
    if vendor_id:
        v_rows = sb.table("vendors").select("*").eq("id", vendor_id).limit(1).execute().data or []
        if v_rows:
            resolved_vendor_name = v_rows[0]["canonical_name"]
            vendor_zoho_id = v_rows[0].get("zoho_contact_id")
    elif resolved_vendor_name:
        v_rows = sb.table("vendors").select("*").ilike("canonical_name", f"%{resolved_vendor_name}%").limit(1).execute().data or []
        if v_rows:
            vendor_id = v_rows[0]["id"]
            resolved_vendor_name = v_rows[0]["canonical_name"]
            vendor_zoho_id = v_rows[0].get("zoho_contact_id")

    if mode == "paid_now" and not payment_account_id:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Payment account is required for Paid Now load."}}
    if mode == "vendor_udhaar" and not resolved_vendor_name:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Vendor is required for Udhaar / Credit load."}}

    all_skus = sb.table("inventory_items").select("*").eq("is_active", True).execute().data or []
    prepared: list[dict[str, Any]] = []
    for raw in lines:
        dest = "order" if str(raw.get("destination") or "stock").lower() == "order" else "stock"
        qty, parsed_u = parse_positive_quantity(raw.get("quantity"), default=0.0)
        if qty <= 0:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Quantity must be > 0."}}
        unit = normalize_inventory_unit(raw.get("unit") or parsed_u or "pcs")
        raw_name = str(raw.get("material_name") or "").strip()

        sku: dict[str, Any] | None = None
        if dest == "stock":
            sku = match_inventory_sku(sb, _clean_opt_id(raw.get("inventory_item_id")) or raw_name, items_cache=all_skus)
            mat_label = sku["name"] if sku else raw_name
            if not mat_label:
                return {"ok": False, "error_kind": "validation_400", "error": {"message": "Material name is required."}}
            cat = (sku["category"] if sku else None) or raw.get("category") or infer_material_category(mat_label)
            unit = sku["unit"] if sku else unit
        else:
            pid = _clean_opt_id(raw.get("project_id")) or ""
            if not pid:
                return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer order is required for direct order line."}}
            mat_label = raw_name
            if not mat_label:
                return {"ok": False, "error_kind": "validation_400", "error": {"message": "Material name is required for customer line."}}
            cat = raw.get("category") or infer_material_category(mat_label)

        u_rate: float | None = None
        tot_inr: float | None = None
        if mode != "rate_pending":
            if raw.get("unit_rate_inr") is not None and str(raw.get("unit_rate_inr")) != "":
                u_rate = round(float(raw["unit_rate_inr"]), 2)
                tot_inr = round(qty * u_rate, 2)
            elif raw.get("total_inr") is not None and str(raw.get("total_inr")) != "":
                tot_inr = round(float(raw["total_inr"]), 2)
                u_rate = round(tot_inr / qty, 2) if qty > 0 else 0.0
            else:
                return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Rate ₹ required for '{mat_label}' (or use Rate Pending)."}}

        prepared.append({
            **raw,
            "destination": dest,
            "project_id": _clean_opt_id(raw.get("project_id")),
            "item_id": _clean_opt_id(raw.get("item_id")),
            "matched_item_material_id": _clean_opt_id(raw.get("matched_item_material_id")),
            "sku": sku,
            "material_label": mat_label,
            "category": cat,
            "quantity": qty,
            "unit": unit,
            "unit_rate_inr": u_rate,
            "total_inr": tot_inr,
        })

    batch_id = str(uuid.uuid4())
    ref_no = reference_number or make_zoho_reference_number(batch_id, 0)
    zoho_res: dict[str, Any] = {"ok": True, "stubbed": True, "zoho_type": None, "zoho_id": None}
    if mode != "rate_pending":
        zoho_res = await zoho.push_mixed_inventory_load_to_zoho(
            sb,
            settlement_mode=mode,
            lines=prepared,
            reference_number=ref_no,
            payment_account_id=payment_account_id,
            vendor_name=resolved_vendor_name,
            vendor_zoho_id=vendor_zoho_id,
            vendor_phone=vendor_phone,
            vendor_gstin=vendor_gstin,
            invoice_no=invoice_no,
            txn_date=sanitize_iso_date(txn_date),
            notes=(notes or "").strip(),
        )
        if not zoho_res.get("ok"):
            return zoho_res
        vendor_id = vendor_id or _clean_opt_id(zoho_res.get("vendor_id"))

    z_status = "rate_pending" if mode == "rate_pending" else ("stubbed" if zoho_res.get("stubbed") else "synced")
    created_movements: list[dict[str, Any]] = []
    undo_event_ids: list[str] = []

    for idx, ln in enumerate(prepared):
        line_ref = ref_no if idx == 0 else f"{ref_no}-L{idx}"
        if ln["destination"] == "stock":
            sku = ln["sku"]
            created_sku = False
            if not sku:
                sku_res = upsert_inventory_item(
                    sb, name=ln["material_label"], category=ln["category"], unit=ln["unit"],
                    avg_unit_cost_inr=ln["unit_rate_inr"] or 0.0,
                )
                sku = sku_res["item"]
                all_skus.append(sku)
                created_sku = True
            old_qty = float(sku.get("on_hand_qty") or 0)
            old_avg = float(sku.get("avg_unit_cost_inr") or 0)
            old_last = float(sku.get("last_purchase_rate_inr") or 0)
            if mode == "rate_pending":
                new_qty = round(old_qty + ln["quantity"], 2)
                new_avg, new_last = old_avg, old_last
            else:
                new_qty, new_avg, _ = compute_weighted_avg_load(old_qty, old_avg, ln["quantity"], ln["unit_rate_inr"] or 0.0)
                new_last = round(float(ln["unit_rate_inr"] or 0), 2)
            sb.table("inventory_items").update({
                "on_hand_qty": new_qty, "avg_unit_cost_inr": new_avg, "last_purchase_rate_inr": new_last
            }).eq("id", sku["id"]).execute()
            sku["on_hand_qty"], sku["avg_unit_cost_inr"], sku["last_purchase_rate_inr"] = new_qty, new_avg, new_last

            mov_payload = {
                "batch_id": batch_id,
                "inventory_item_id": sku["id"],
                "movement_type": "stock_load",
                "qty_delta": ln["quantity"],
                "unit": ln["unit"],
                "unit_rate_inr": ln["unit_rate_inr"],
                "total_inr": ln["total_inr"],
                "settlement_mode": mode,
                "payment_account_id": payment_account_id,
                "vendor_id": vendor_id,
                "invoice_no": invoice_no,
                "material_label": sku["name"],
                "category": ln["category"],
                "zoho_sync_status": z_status,
                "zoho_txn_type": zoho_res.get("zoho_type"),
                "zoho_txn_id": zoho_res.get("zoho_id"),
                "zoho_reference_number": line_ref if mode != "rate_pending" else None,
                "actor_person_id": actor_person_id,
                "source_channel": "telegram" if source_channel == "telegram" else "web",
                "notes": (notes or "").strip() or None,
                "metadata": {"undo_snapshot": {
                    "sku_id": sku["id"], "created_sku": created_sku,
                    "before_sku": {"on_hand_qty": old_qty, "avg_unit_cost_inr": old_avg, "last_purchase_rate_inr": old_last},
                }},
            }
            ins = sb.table("inventory_movements").insert(mov_payload).execute().data or []
            if ins:
                created_movements.append(ins[0])
        else:
            ful = apply_order_material_fulfillment(
                sb,
                project_id=str(ln["project_id"]),
                incoming_name=ln["material_label"],
                received_qty=ln["quantity"],
                unit=ln["unit"],
                category=ln["category"],
                matched_item_material_id=ln.get("matched_item_material_id"),
                item_id=ln.get("item_id"),
                notes=notes,
                actor_person_id=actor_person_id,
                source_channel=source_channel,
            )
            undo_event_ids.extend(ful["undo_meta"].get("material_track_event_ids") or [])
            cost_delta = _adjust_project_cost_cache(sb, str(ln["project_id"]), ln["total_inr"] or 0.0) if mode != "rate_pending" else 0.0
            mov_payload = {
                "batch_id": batch_id,
                "inventory_item_id": None,
                "movement_type": "direct_order_receipt",
                "qty_delta": ln["quantity"],
                "unit": ln["unit"],
                "unit_rate_inr": ln["unit_rate_inr"],
                "total_inr": ln["total_inr"],
                "settlement_mode": mode,
                "payment_account_id": payment_account_id,
                "vendor_id": vendor_id,
                "invoice_no": invoice_no,
                "project_id": str(ln["project_id"]),
                "item_id": ln.get("item_id"),
                "item_material_id": ful.get("item_material_id"),
                "material_label": ln["material_label"],
                "category": ln["category"],
                "zoho_sync_status": z_status,
                "zoho_txn_type": zoho_res.get("zoho_type"),
                "zoho_txn_id": zoho_res.get("zoho_id"),
                "zoho_reference_number": line_ref if mode != "rate_pending" else None,
                "actor_person_id": actor_person_id,
                "source_channel": "telegram" if source_channel == "telegram" else "web",
                "notes": (notes or "").strip() or None,
                "metadata": {"undo_snapshot": {"order_fulfill": ful["undo_meta"], "project_cost_delta": cost_delta}},
            }
            ins = sb.table("inventory_movements").insert(mov_payload).execute().data or []
            if ins:
                created_movements.append(ins[0])

    if mode == "paid_now" and not zoho_res.get("stubbed"):
        await zoho.best_effort_sync_after_mutation(sb)

    total_val = round(sum(float(ln.get("total_inr") or 0) for ln in prepared), 2)
    summary_val = "Rate Pending" if mode == "rate_pending" else format_inr(total_val)
    return {
        "ok": True,
        "batch_id": batch_id,
        "movements": created_movements,
        "zoho_id": zoho_res.get("zoho_id"),
        "zoho_type": zoho_res.get("zoho_type"),
        "zoho_sync_status": z_status,
        "reference_number": ref_no,
        "undo_event_ids": undo_event_ids,
        "receipt": f"📥 Loaded *{len(created_movements)} line(s)* ({summary_val}) · `{mode.replace('_', ' ').title()}`",
    }


async def complete_rate_pending_movement(
    sb: Any,
    *,
    movement_id: str,
    settlement_mode: str,
    unit_rate_inr: float | None = None,
    line_rates: dict[str, float] | None = None,
    payment_account_id: str | None = None,
    vendor_id: str | None = None,
    vendor_name: str | None = None,
    vendor_phone: str | None = None,
    vendor_gstin: str | None = None,
    invoice_no: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Price an unpriced Rate Pending receipt and push to Zoho WITHOUT re-adding physical stock qty."""
    mode = (settlement_mode or "manual").strip().lower()
    if mode not in ("paid_now", "vendor_udhaar", "manual"):
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Invalid settlement mode."}}

    cur_rows = sb.table("inventory_movements").select("*").eq("id", movement_id).limit(1).execute().data or []
    if not cur_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Movement not found."}}
    cur = cur_rows[0]
    if cur.get("is_undone") or cur.get("zoho_sync_status") != "rate_pending":
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Movement is not Rate Pending."}}

    if line_rates:
        batch_rows = [
            r
            for r in (sb.table("inventory_movements").select("*").eq("batch_id", cur["batch_id"]).execute().data or [])
            if r.get("zoho_sync_status") == "rate_pending" and not r.get("is_undone") and r["id"] in line_rates
        ]
    else:
        batch_rows = [cur]

    resolved_vendor_name = (vendor_name or "").strip()
    vendor_zoho_id = None
    eff_vendor_id = _clean_opt_id(vendor_id) or cur.get("vendor_id")
    if eff_vendor_id:
        vr = sb.table("vendors").select("*").eq("id", eff_vendor_id).limit(1).execute().data or []
        if vr:
            resolved_vendor_name = vr[0]["canonical_name"]
            vendor_zoho_id = vr[0].get("zoho_contact_id")
    elif resolved_vendor_name:
        vr = sb.table("vendors").select("*").ilike("canonical_name", f"%{resolved_vendor_name}%").limit(1).execute().data or []
        if vr:
            eff_vendor_id = vr[0]["id"]
            resolved_vendor_name = vr[0]["canonical_name"]
            vendor_zoho_id = vr[0].get("zoho_contact_id")

    eff_acc_id = _clean_opt_id(payment_account_id) or cur.get("payment_account_id")
    if mode == "paid_now" and not eff_acc_id:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Payment account is required."}}
    if mode == "vendor_udhaar" and not resolved_vendor_name:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Vendor is required for Udhaar."}}

    z_lines: list[dict[str, Any]] = []
    for r in batch_rows:
        rate = float(line_rates[r["id"]]) if line_rates else float(unit_rate_inr or 0)
        if rate <= 0:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Unit rate ₹ must be greater than 0."}}
        qty = abs(float(r.get("qty_delta") or 0))
        tot = round(qty * rate, 2)
        z_lines.append({
            "row": r,
            "destination": "order" if r.get("movement_type") == "direct_order_receipt" else "stock",
            "project_id": r.get("project_id"),
            "material_label": r["material_label"],
            "quantity": qty,
            "unit": r["unit"],
            "unit_rate_inr": round(rate, 2),
            "total_inr": tot,
        })

    ref_no = make_zoho_reference_number(cur["id"], 1)
    zoho_res = await zoho.push_mixed_inventory_load_to_zoho(
        sb,
        settlement_mode=mode,
        lines=z_lines,
        reference_number=ref_no,
        payment_account_id=eff_acc_id,
        vendor_name=resolved_vendor_name,
        vendor_zoho_id=vendor_zoho_id,
        invoice_no=invoice_no or cur.get("invoice_no"),
        notes=(notes or cur.get("notes") or "").strip(),
    )
    if not zoho_res.get("ok"):
        return zoho_res

    z_status = "stubbed" if zoho_res.get("stubbed") else "synced"
    for idx, zl in enumerate(z_lines):
        r = zl["row"]
        rate = zl["unit_rate_inr"]
        tot = zl["total_inr"]
        qty = zl["quantity"]
        cost_delta = 0.0
        if r.get("inventory_item_id"):
            skus = sb.table("inventory_items").select("*").eq("id", r["inventory_item_id"]).limit(1).execute().data or []
            if skus:
                sku = skus[0]
                cur_on_hand = float(sku.get("on_hand_qty") or 0)
                cur_avg = float(sku.get("avg_unit_cost_inr") or 0)
                if cur_on_hand <= 0:
                    # Keep zero-stock = ₹0.00 value invariant even if stock was consumed before pricing
                    new_avg = 0.0
                else:
                    prior_qty = max(round(cur_on_hand - qty, 2), 0.0)
                    _, new_avg, _ = compute_weighted_avg_load(prior_qty, cur_avg, qty, rate)
                # Deliberately do NOT change on_hand_qty (physical qty was already loaded on Gate-In)!
                sb.table("inventory_items").update({
                    "avg_unit_cost_inr": new_avg,
                    "last_purchase_rate_inr": rate,
                }).eq("id", sku["id"]).execute()
        elif r.get("project_id"):
            cost_delta = _adjust_project_cost_cache(sb, r["project_id"], tot)

        meta_upd = dict(r.get("metadata") or {})
        if cost_delta:
            snap = dict(meta_upd.get("undo_snapshot") or {})
            snap["project_cost_delta"] = round(float(snap.get("project_cost_delta") or 0) + cost_delta, 2)
            meta_upd["undo_snapshot"] = snap

        line_ref = ref_no if idx == 0 else f"{ref_no}-L{idx}"
        sb.table("inventory_movements").update({
            "unit_rate_inr": rate,
            "total_inr": tot,
            "settlement_mode": mode,
            "payment_account_id": eff_acc_id,
            "vendor_id": eff_vendor_id,
            "invoice_no": invoice_no or cur.get("invoice_no"),
            "zoho_sync_status": z_status,
            "zoho_txn_type": zoho_res.get("zoho_type"),
            "zoho_txn_id": zoho_res.get("zoho_id"),
            "zoho_reference_number": line_ref,
            "metadata": meta_upd,
            **({"notes": notes.strip()} if notes and notes.strip() else {}),
        }).eq("id", r["id"]).execute()

    if mode == "paid_now" and not zoho_res.get("stubbed"):
        await zoho.best_effort_sync_after_mutation(sb)

    total_sum = round(sum(zl["total_inr"] for zl in z_lines), 2)
    return {
        "ok": True,
        "zoho_sync_status": z_status,
        "zoho_id": zoho_res.get("zoho_id"),
        "zoho_type": zoho_res.get("zoho_type"),
        "total_inr": total_sum,
        "receipt": f"💰 Rate entered ({format_inr(total_sum)}) → Zoho `{zoho_res.get('zoho_type')}`",
    }


async def execute_customer_stock_attribution(
    sb: Any,
    *,
    inventory_item_id: str,
    project_id: str,
    quantity: float,
    item_id: str | None = None,
    item_material_id: str | None = None,
    notes: str | None = None,
    reference_number: str | None = None,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Pull reusable stock from godown onto a Customer Order (item optional) and post Zoho COGS Journal."""
    existing_movs = _find_existing_by_ref(sb, reference_number)
    if existing_movs:
        return {
            "ok": True,
            "deduped": True,
            "movement": existing_movs[0],
            "issue_cost_inr": float(existing_movs[0].get("total_inr") or 0),
            "zoho_id": existing_movs[0].get("zoho_txn_id"),
            "reference_number": reference_number,
            "receipt": "Already attributed.",
        }

    inventory_item_id = _clean_opt_id(inventory_item_id) or ""
    project_id = _clean_opt_id(project_id) or ""
    item_id = _clean_opt_id(item_id)
    item_material_id = _clean_opt_id(item_material_id)
    actor_person_id = _clean_opt_id(actor_person_id)

    sku_rows = sb.table("inventory_items").select("*").eq("id", inventory_item_id).limit(1).execute().data or []
    if not sku_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Inventory SKU not found."}}
    sku = sku_rows[0]

    prj_rows = sb.table("projects").select("id,project_code,customer_name").eq("id", project_id).limit(1).execute().data or []
    if not prj_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer order not found."}}
    prj = prj_rows[0]

    old_qty = float(sku.get("on_hand_qty") or 0)
    old_avg = float(sku.get("avg_unit_cost_inr") or 0)
    old_last = float(sku.get("last_purchase_rate_inr") or 0)
    req_qty = round(float(quantity or 0), 2)
    if req_qty <= 0 or req_qty > old_qty + 1e-6:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": f"Only {old_qty:g} {sku['unit']} of '{sku['name']}' available in stock."},
        }

    new_qty, new_avg, issue_cost = compute_issue_cost_and_remaining(old_qty, old_avg, req_qty)
    mov_id = str(uuid.uuid4())
    ref_no = reference_number or make_zoho_reference_number(mov_id, 0)
    zoho_res = await zoho.push_inventory_journal_to_zoho(
        sb,
        amount_inr=issue_cost,
        direction="attribute_to_customer",
        reference_number=ref_no,
        notes=f"{sku['name']} ({req_qty:g} {sku['unit']}) for {prj['customer_name']} {notes or ''}".strip(),
        project_id=project_id,
    )
    if not zoho_res.get("ok"):
        return zoho_res

    sb.table("inventory_items").update({"on_hand_qty": new_qty, "avg_unit_cost_inr": new_avg}).eq("id", sku["id"]).execute()
    ful = apply_order_material_fulfillment(
        sb,
        project_id=project_id,
        incoming_name=sku["name"],
        received_qty=req_qty,
        unit=sku["unit"],
        category=sku["category"],
        matched_item_material_id=item_material_id,
        item_id=item_id,
        inventory_item_id=sku["id"],
        notes=notes,
        actor_person_id=actor_person_id,
        source_channel=source_channel,
    )
    cost_delta = _adjust_project_cost_cache(sb, project_id, issue_cost)
    z_status = "not_applicable" if issue_cost <= 0 else ("stubbed" if zoho_res.get("stubbed") else "synced")
    ins = sb.table("inventory_movements").insert({
        "id": mov_id,
        "batch_id": mov_id,
        "inventory_item_id": sku["id"],
        "movement_type": "customer_attribution",
        "qty_delta": -req_qty,
        "unit": sku["unit"],
        "unit_rate_inr": old_avg,
        "total_inr": issue_cost,
        "settlement_mode": "attribution",
        "project_id": project_id,
        "item_id": item_id,
        "item_material_id": ful.get("item_material_id"),
        "material_label": sku["name"],
        "category": sku["category"],
        "zoho_sync_status": z_status,
        "zoho_txn_type": zoho_res.get("zoho_type"),
        "zoho_txn_id": zoho_res.get("zoho_id"),
        "zoho_reference_number": ref_no,
        "actor_person_id": actor_person_id,
        "source_channel": "telegram" if source_channel == "telegram" else "web",
        "notes": (notes or "").strip() or None,
        "metadata": {"undo_snapshot": {
            "sku_id": sku["id"],
            "before_sku": {"on_hand_qty": old_qty, "avg_unit_cost_inr": old_avg, "last_purchase_rate_inr": old_last},
            "order_fulfill": ful["undo_meta"],
            "project_cost_delta": cost_delta,
        }},
    }).execute().data or []

    return {
        "ok": True,
        "movement": ins[0] if ins else None,
        "issue_cost_inr": issue_cost,
        "new_on_hand_qty": new_qty,
        "zoho_id": zoho_res.get("zoho_id"),
        "reference_number": ref_no,
        "undo_event_ids": ful["undo_meta"].get("material_track_event_ids") or [],
        "receipt": f"📤 Pulled *{req_qty:g} {sku['unit']}* of *{sku['name']}* ({format_inr(issue_cost)}) → *{prj['customer_name']}* (`#{prj['project_code']}`)",
    }


async def execute_inventory_count_adjust(
    sb: Any,
    *,
    inventory_item_id: str,
    actual_qty: float,
    notes: str | None = None,
    reference_number: str | None = None,
    actor_person_id: str | None = None,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Adjust stock to actual physical count and write off variance to General Shop COGS."""
    existing_movs = _find_existing_by_ref(sb, reference_number)
    if existing_movs:
        return {
            "ok": True,
            "deduped": True,
            "movement": existing_movs[0],
            "delta": float(existing_movs[0].get("qty_delta") or 0),
            "receipt": "Count adjustment already recorded.",
        }

    inventory_item_id = _clean_opt_id(inventory_item_id) or ""
    actor_person_id = _clean_opt_id(actor_person_id)

    sku_rows = sb.table("inventory_items").select("*").eq("id", inventory_item_id).limit(1).execute().data or []
    if not sku_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Inventory SKU not found."}}
    sku = sku_rows[0]

    old_qty = float(sku.get("on_hand_qty") or 0)
    old_avg = float(sku.get("avg_unit_cost_inr") or 0)
    old_last = float(sku.get("last_purchase_rate_inr") or 0)
    target_qty = round(max(float(actual_qty or 0), 0.0), 2)
    delta = round(target_qty - old_qty, 2)
    if abs(delta) < 1e-6:
        return {"ok": True, "delta": 0.0, "receipt": f"✓ *{sku['name']}* count unchanged at {target_qty:g} {sku['unit']}"}

    mov_id = str(uuid.uuid4())
    ref_no = reference_number or make_zoho_reference_number(mov_id, 0)
    if delta < 0:
        new_qty, new_avg, adj_cost = compute_issue_cost_and_remaining(old_qty, old_avg, abs(delta))
        direction = "shrinkage_writeoff"
        rate_used = old_avg
    else:
        rate_used = old_avg if old_avg > 0 else old_last
        new_qty, new_avg, adj_cost = compute_weighted_avg_load(old_qty, old_avg, delta, rate_used)
        direction = "count_gain"

    zoho_res = await zoho.push_inventory_journal_to_zoho(
        sb,
        amount_inr=adj_cost,
        direction=direction,
        reference_number=ref_no,
        notes=f"Count Adjust {sku['name']} ({delta:+g} {sku['unit']}) {notes or ''}".strip(),
    )
    if not zoho_res.get("ok"):
        return zoho_res

    sb.table("inventory_items").update({"on_hand_qty": new_qty, "avg_unit_cost_inr": new_avg}).eq("id", sku["id"]).execute()
    z_status = "not_applicable" if adj_cost <= 0 else ("stubbed" if zoho_res.get("stubbed") else "synced")
    ins = sb.table("inventory_movements").insert({
        "id": mov_id,
        "batch_id": mov_id,
        "inventory_item_id": sku["id"],
        "movement_type": "count_adjust",
        "qty_delta": delta,
        "unit": sku["unit"],
        "unit_rate_inr": rate_used,
        "total_inr": adj_cost,
        "settlement_mode": "count_adjust",
        "material_label": sku["name"],
        "category": sku["category"],
        "zoho_sync_status": z_status,
        "zoho_txn_type": zoho_res.get("zoho_type"),
        "zoho_txn_id": zoho_res.get("zoho_id"),
        "zoho_reference_number": ref_no,
        "actor_person_id": actor_person_id,
        "source_channel": "telegram" if source_channel == "telegram" else "web",
        "notes": (notes or "").strip() or None,
        "metadata": {"undo_snapshot": {
            "sku_id": sku["id"],
            "before_sku": {"on_hand_qty": old_qty, "avg_unit_cost_inr": old_avg, "last_purchase_rate_inr": old_last},
        }},
    }).execute().data or []

    return {
        "ok": True,
        "movement": ins[0] if ins else None,
        "delta": delta,
        "new_on_hand_qty": new_qty,
        "receipt": f"⚖️ Count adjusted *{sku['name']}*: {old_qty:g} → *{new_qty:g} {sku['unit']}* ({format_inr(adj_cost)})",
    }


async def undo_inventory_movement(
    sb: Any, movement_id: str, *, actor_person_id: str | None = None
) -> dict[str, Any]:
    """Same-day IST undo for an inventory movement/batch: reverses Zoho entry and restores stock & order lines."""
    cur_rows = sb.table("inventory_movements").select("*").eq("id", movement_id).limit(1).execute().data or []
    if not cur_rows:
        return {"ok": False, "error": "Inventory movement not found."}
    cur = cur_rows[0]
    if cur.get("is_undone"):
        return {"ok": True, "already_undone": True, "receipt": "Already undone."}
    if not _is_same_day_ist(cur.get("created_at")):
        return {"ok": False, "error": "Same-day IST undo window expired; use ± Count Adjust instead."}

    batch_rows = [
        r
        for r in (sb.table("inventory_movements").select("*").eq("batch_id", cur["batch_id"]).execute().data or [])
        if not r.get("is_undone")
    ]
    z_deleted: set[str] = set()
    for r in batch_rows:
        zid = str(r.get("zoho_txn_id") or "").strip()
        ztype = str(r.get("zoho_txn_type") or "").strip()
        if zid and zid not in z_deleted and r.get("zoho_sync_status") in ("synced", "stubbed"):
            z_res = await zoho.delete_zoho_financial_entry(zoho_id=zid, zoho_type=ztype)
            if not z_res.get("ok"):
                return {"ok": False, "error": z_res.get("error") or "Failed to reverse Zoho entry."}
            z_deleted.add(zid)

    now_iso = datetime.now(timezone.utc).isoformat()
    for r in reversed(batch_rows):
        snap = (r.get("metadata") or {}).get("undo_snapshot") or {}
        sku_id = snap.get("sku_id") or r.get("inventory_item_id")
        if sku_id and snap.get("before_sku"):
            sb.table("inventory_items").update(snap["before_sku"]).eq("id", sku_id).execute()
            if snap.get("created_sku") and float(snap["before_sku"].get("on_hand_qty") or 0) == 0:
                sb.table("inventory_items").update({"is_active": False}).eq("id", sku_id).execute()

        ful = snap.get("order_fulfill") or {}
        if ful.get("created_new_id"):
            sb.table("item_materials").delete().eq("id", ful["created_new_id"]).execute()
        if ful.get("created_remainder_id"):
            sb.table("item_materials").delete().eq("id", ful["created_remainder_id"]).execute()
        if ful.get("before_row"):
            br = ful["before_row"]
            sb.table("item_materials").update({
                "quantity": br["quantity"], "status": br["status"], "notes": br.get("notes"),
                "inventory_item_id": br.get("inventory_item_id"),
            }).eq("id", br["id"]).execute()
        if ful.get("material_track_event_ids"):
            undo_stage_events(sb, ful["material_track_event_ids"], actor_person_id=actor_person_id)
        if ful.get("project_id") and snap.get("project_cost_delta"):
            _adjust_project_cost_cache(sb, ful["project_id"], -float(snap["project_cost_delta"]))

        sb.table("inventory_movements").update({
            "is_undone": True, "undone_at": now_iso, "undone_by_person_id": actor_person_id
        }).eq("id", r["id"]).execute()

    if z_deleted:
        await zoho.best_effort_sync_after_mutation(sb)

    return {"ok": True, "reverted_count": len(batch_rows), "receipt": f"↩️ Reverted {len(batch_rows)} inventory entry(s)."}
