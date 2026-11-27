"""Lean 2-Tool Inventory AI handlers, slot resolvers, Zoho-safety boundaries & undo hooks."""

from __future__ import annotations

import json
from typing import Any

from app.services import step_executors
from app.services.goal_primitives import format_inr, make_zoho_reference_number

INVENTORY_READ_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_inventory",
        "description": (
            "Search Workshop Stock SKUs (on-hand qty, avg rate ₹, low-stock alerts), "
            "consolidated To-Buy demand across active orders, and Rate Pending gate receipts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Material name or Hinglish alias (empty to list top SKUs)"},
                "low_stock_only": {"type": "boolean"},
                "include_to_buy": {"type": "boolean"},
                "include_rate_pending": {"type": "boolean"},
            },
        },
    }
]

INVENTORY_ACTION_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "stage_inventory_action",
        "description": (
            "Default tool for ALL itemized material/product supplier bills (photos/PDFs/text) and Workshop Inventory operations: "
            "1) 'load' (loads Workshop Stock or direct Customer Order lines AND registers the Zoho Open AP Bill ['vendor_udhaar'] "
            "or Zoho Expense ['paid_now'] in one step; also supports 'manual' and 'rate_pending' for unpriced challans); "
            "2) 'attribute' (CUJ 1 pull workshop stock onto a Customer Order, furniture item optional); "
            "3) 'count_adjust' (set actual physical count on hand and write off shrinkage/variance)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_kind": {"type": "string", "enum": ["load", "attribute", "count_adjust"]},
                "settlement_mode": {"type": "string", "enum": ["paid_now", "vendor_udhaar", "manual", "rate_pending"]},
                "payment_account_code": {"type": "string", "description": "Account code for paid_now loads (e.g. 'axis', 'cash_galla', 'float_ansh')"},
                "party_name": {"type": "string", "description": "Supplier/vendor name for loads"},
                "phone": {"type": "string", "description": "Supplier phone from bill header if present"},
                "gstin": {"type": "string", "description": "Supplier GSTIN from bill header if present"},
                "invoice_no": {"type": "string"},
                "txn_date": {"type": "string", "description": "YYYY-MM-DD"},
                "inventory_item_id": {"type": "string", "description": "SKU UUID or name for attribute/count_adjust"},
                "material_name": {"type": "string", "description": "Material/SKU name if inventory_item_id not known"},
                "project_id": {"type": "string", "description": "Customer order UUID/code/name for attribute"},
                "item_name": {"type": "string", "description": "Optional furniture item name ('Kitchen', 'Master Bed')"},
                "matched_item_material_id": {"type": "string", "description": "Open item_materials UUID being fulfilled if matched"},
                "quantity": {"type": "number", "description": "Quantity pulled for attribute"},
                "actual_qty": {"type": "number", "description": "Physical shop count for count_adjust"},
                "lines": {
                    "type": "array",
                    "description": "Line items for action_kind='load' (mixed stock and/or direct customer lines)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "destination": {"type": "string", "enum": ["stock", "order"]},
                            "material_name": {"type": "string"},
                            "inventory_item_id": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "unit_rate_inr": {"type": "number"},
                            "total_inr": {"type": "number"},
                            "project_id": {"type": "string"},
                            "item_name": {"type": "string"},
                            "matched_item_material_id": {"type": "string"},
                        },
                        "required": ["material_name", "quantity"],
                    },
                },
                "notes": {"type": "string"},
                "clarification_question": {"type": "string"},
                "allow_missing_clarification": {"type": "boolean"},
            },
            "required": ["action_kind"],
        },
    }
]

INVENTORY_OPS = frozenset({"inventory_load", "inventory_attribution", "inventory_count_adjust"})


def execute_inventory_read_tool(sb: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    data = step_executors.get_inventory_overview_data(sb)
    q = str(arguments.get("query") or "").strip().lower()
    items = data["items"]
    if q:
        items = [
            it
            for it in items
            if q in " ".join([it.get("name") or "", it.get("category_label") or "", *(it.get("aliases") or [])]).lower()
        ]
    if arguments.get("low_stock_only"):
        items = [it for it in items if it.get("is_low_stock")]

    out: dict[str, Any] = {"kpis": data["kpis"], "stock_items": items[:25]}
    if arguments.get("include_to_buy"):
        tb = data["to_buy"]
        if q:
            tb = [r for r in tb if q in str(r.get("material_name") or "").lower()]
        out["to_buy"] = tb[:20]
    if arguments.get("include_rate_pending"):
        out["rate_pending_movements"] = [
            m for m in data["movements"] if m.get("zoho_sync_status") == "rate_pending" and not m.get("is_undone")
        ][:15]
    return out


def build_inventory_candidates_from_tool(args: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(args.get("action_kind") or "load").strip().lower()
    op_map = {
        "load": "inventory_load",
        "attribute": "inventory_attribution",
        "count_adjust": "inventory_count_adjust",
    }
    op = op_map.get(kind, "inventory_load")
    lines = args.get("lines") or []
    mat_lbl = str(args.get("material_name") or "").strip()
    if op == "inventory_load" and not lines and mat_lbl and args.get("quantity"):
        lines = [{
            "destination": "order" if args.get("project_id") else "stock",
            "material_name": mat_lbl,
            "quantity": args.get("quantity"),
            "unit": args.get("unit") or "pcs",
            "unit_rate_inr": args.get("unit_rate_inr"),
            "project_id": args.get("project_id"),
            "item_name": args.get("item_name"),
            "matched_item_material_id": args.get("matched_item_material_id"),
        }]
    summary = (
        f"Inventory Load ({len(lines)} line{'s' if len(lines) != 1 else ''})"
        if op == "inventory_load"
        else (f"Pull Stock: {mat_lbl or 'Item'}" if op == "inventory_attribution" else f"Count Adjust: {mat_lbl or 'Item'}")
    )
    return [{
        "operation_kind": op,
        "summary_title": summary,
        "settlement_mode": args.get("settlement_mode") or "manual",
        "payment_account_code": args.get("payment_account_code"),
        "party_name": args.get("party_name"),
        "phone": args.get("phone"),
        "gstin": args.get("gstin"),
        "invoice_no": args.get("invoice_no"),
        "txn_date": args.get("txn_date"),
        "inventory_item_id": args.get("inventory_item_id"),
        "full_name": mat_lbl or None,
        "project_id": args.get("project_id"),
        "item_name": args.get("item_name"),
        "matched_item_material_id": args.get("matched_item_material_id"),
        "quantity": args.get("quantity"),
        "actual_qty": args.get("actual_qty"),
        "inventory_lines_json": json.dumps(lines, ensure_ascii=False) if lines else None,
        "notes": args.get("notes"),
        "clarification_question": args.get("clarification_question"),
    }]


def resolve_inventory_slots(sb: Any, slots: dict[str, Any], resolve_project_fn: Any) -> None:
    op = str(slots.get("operation_kind") or "")
    if op not in INVENTORY_OPS:
        return

    if op in ("inventory_attribution", "inventory_count_adjust"):
        sku_needle = slots.get("inventory_item_id") or slots.get("full_name") or slots.get("material_name")
        sku = step_executors.match_inventory_sku(sb, str(sku_needle or ""))
        if sku:
            slots["inventory_item_id"] = sku["id"]
            slots["full_name"] = sku["name"]
            slots.setdefault("unit", sku["unit"])
        else:
            slots.pop("inventory_item_id", None)

    if op in ("inventory_attribution", "inventory_load") and slots.get("project_id"):
        raw_pid = str(slots["project_id"])
        resolved_pid = resolve_project_fn(sb, raw_pid)
        if resolved_pid:
            slots["project_id"] = resolved_pid
        else:
            direct = sb.table("projects").select("id").eq("id", raw_pid).limit(1).execute().data
            if not direct:
                slots.pop("project_id", None)

    if op == "inventory_attribution" and slots.get("project_id") and slots.get("item_name") and not slots.get("item_id"):
        it_rows = (
            sb.table("project_items")
            .select("id")
            .eq("project_id", slots["project_id"])
            .ilike("item_name", str(slots["item_name"]).strip())
            .limit(1)
            .execute()
            .data
            or []
        )
        if it_rows:
            slots["item_id"] = it_rows[0]["id"]

    if slots.get("inventory_lines_json"):
        try:
            raw_lines = json.loads(str(slots["inventory_lines_json"]))
        except (TypeError, ValueError):
            raw_lines = []
        if isinstance(raw_lines, list):
            top_pid = slots.get("project_id") if op == "inventory_load" else None
            for ln in raw_lines:
                if not isinstance(ln, dict):
                    continue
                if top_pid and not ln.get("project_id"):
                    ln["destination"] = "order"
                    ln["project_id"] = top_pid
                if ln.get("destination") == "order" and ln.get("project_id"):
                    resolved_pid = resolve_project_fn(sb, str(ln["project_id"]))
                    if resolved_pid:
                        ln["project_id"] = resolved_pid
            slots["inventory_lines_json"] = json.dumps(raw_lines, ensure_ascii=False)


def compute_inventory_missing_slots(op: str, slots: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if op == "inventory_load":
        lines = _parse_lines(slots)
        if not lines:
            missing.append("inventory_lines_json")
        mode = str(slots.get("settlement_mode") or "manual").lower()
        if mode == "paid_now" and not slots.get("payment_account_id"):
            missing.append("payment_account_code")
        elif mode == "vendor_udhaar" and not slots.get("party_name"):
            missing.append("party_name")
        if mode != "rate_pending" and lines:
            if any(ln.get("unit_rate_inr") in (None, "") and ln.get("total_inr") in (None, "") for ln in lines):
                missing.append("unit_rate_inr")
    elif op == "inventory_attribution":
        if not slots.get("inventory_item_id"):
            missing.append("inventory_item_id")
        if not slots.get("project_id"):
            missing.append("project_id")
        if not slots.get("quantity") or float(slots.get("quantity") or 0) <= 0:
            missing.append("quantity")
    elif op == "inventory_count_adjust":
        if not slots.get("inventory_item_id"):
            missing.append("inventory_item_id")
        if slots.get("actual_qty") is None:
            missing.append("actual_qty")
    return missing


def is_inventory_auto_commit(op: str, slots: dict[str, Any]) -> bool:
    """Pure physical Rate Pending Gate-Ins auto-commit with Undo; all Zoho-touching moves require Confirm."""
    return op == "inventory_load" and str(slots.get("settlement_mode") or "").lower() == "rate_pending"


def format_inventory_confirmation_summary(sb: Any, op: str, slots: dict[str, Any]) -> str:
    if op == "inventory_load":
        mode = str(slots.get("settlement_mode") or "manual").replace("_", " ").title()
        lines = _parse_lines(slots)
        tot = sum(
            float(ln["total_inr"]) if ln.get("total_inr") is not None else float(ln.get("quantity") or 0) * float(ln.get("unit_rate_inr") or 0)
            for ln in lines
        )
        bullet_lines = []
        for ln in lines[:6]:
            dest_lbl = "Workshop Stock" if ln.get("destination", "stock") == "stock" else "Customer Order"
            rate_str = f" @ {format_inr(float(ln['unit_rate_inr']))}" if ln.get("unit_rate_inr") is not None else ""
            bullet_lines.append(f"• *{ln.get('material_name')}* — {ln.get('quantity')} {ln.get('unit') or 'pcs'}{rate_str} → _{dest_lbl}_")
        v_badge = (
            " _(🆕 New Vendor)_"
            if slots.get("is_new_payee") is True
            else (" _(✅ Matched)_" if (slots.get("is_new_payee") is False or slots.get("vendor_id") or slots.get("zoho_contact_id")) else "")
        )
        extra = f"\n• Vendor: *{slots['party_name']}*{v_badge}" if slots.get("party_name") else ""
        inv = f"\n• Invoice: `{slots['invoice_no']}`" if slots.get("invoice_no") else ""
        acc = f"\n• Account: `{str(slots['payment_account_code']).upper()}`" if slots.get("payment_account_code") else ""
        return (
            f"📦 *Confirm Inventory Load ({mode})*\n"
            + "\n".join(bullet_lines)
            + f"{extra}{inv}{acc}\n• Total: *{format_inr(tot)}*"
        )
    if op == "inventory_attribution":
        sku = step_executors.match_inventory_sku(sb, str(slots.get("inventory_item_id") or ""))
        qty = float(slots.get("quantity") or 0)
        avg = float((sku or {}).get("avg_unit_cost_inr") or 0)
        cost = round(qty * avg, 2)
        prj_rows = sb.table("projects").select("project_code,customer_name").eq("id", slots.get("project_id")).limit(1).execute().data or []
        ord_lbl = f"#{prj_rows[0]['project_code']} — {prj_rows[0]['customer_name']}" if prj_rows else str(slots.get("project_id"))
        return (
            f"📤 *Confirm Stock Attribution to Order*\n"
            f"• Material: *{(sku or {}).get('name') or slots.get('full_name')}* ({qty:g} {(sku or {}).get('unit') or 'pcs'})\n"
            f"• Order: *{ord_lbl}*\n"
            f"• Zoho COGS Journal: *{format_inr(cost)}*"
        )
    sku = step_executors.match_inventory_sku(sb, str(slots.get("inventory_item_id") or ""))
    actual = float(slots.get("actual_qty") or 0)
    old_qty = float((sku or {}).get("on_hand_qty") or 0)
    avg = float((sku or {}).get("avg_unit_cost_inr") or 0)
    delta_cost = round(abs(actual - old_qty) * avg, 2)
    return (
        f"⚖️ *Confirm Stock Count Adjust*\n"
        f"• Material: *{(sku or {}).get('name') or slots.get('full_name')}*\n"
        f"• Count: {old_qty:g} → *{actual:g} {(sku or {}).get('unit') or 'pcs'}*\n"
        f"• Zoho Variance Journal: *{format_inr(delta_cost)}*"
    )


async def run_inventory_goal_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    op: str,
    slots: dict[str, Any],
    actor_person_id: str | None,
    source_channel: str,
) -> dict[str, Any]:
    ref_no = make_zoho_reference_number(goal_id, step_index)
    src = "telegram" if source_channel == "telegram" else "web"
    if op == "inventory_load":
        return await step_executors.execute_inventory_load(
            sb,
            settlement_mode=str(slots.get("settlement_mode") or "manual"),
            lines=_parse_lines(slots),
            payment_account_id=slots.get("payment_account_id"),
            vendor_id=slots.get("vendor_id"),
            vendor_name=slots.get("party_name"),
            vendor_phone=slots.get("phone"),
            vendor_gstin=slots.get("gstin"),
            invoice_no=slots.get("invoice_no"),
            txn_date=slots.get("txn_date"),
            notes=slots.get("notes"),
            reference_number=ref_no,
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    if op == "inventory_attribution":
        return await step_executors.execute_customer_stock_attribution(
            sb,
            inventory_item_id=str(slots["inventory_item_id"]),
            project_id=str(slots["project_id"]),
            quantity=float(slots.get("quantity") or 0),
            item_id=slots.get("item_id"),
            item_material_id=slots.get("matched_item_material_id"),
            notes=slots.get("notes"),
            reference_number=ref_no,
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    return await step_executors.execute_inventory_count_adjust(
        sb,
        inventory_item_id=str(slots["inventory_item_id"]),
        actual_qty=float(slots.get("actual_qty") or 0),
        notes=slots.get("notes"),
        reference_number=ref_no,
        actor_person_id=actor_person_id,
        source_channel=src,
    )


async def undo_inventory_goal_steps(
    sb: Any, goal_row: dict[str, Any], *, actor_person_id: str | None = None
) -> tuple[list[str], list[str]]:
    reverted: list[str] = []
    errors: list[str] = []
    for step in reversed(goal_row.get("steps") or []):
        if step.get("operation") not in INVENTORY_OPS:
            continue
        res = step.get("result") or {}
        if not res.get("ok"):
            continue
        movs = res.get("movements") or ([res["movement"]] if res.get("movement") else [])
        if movs:
            u_res = await step_executors.undo_inventory_movement(
                sb, str(movs[0]["id"]), actor_person_id=actor_person_id
            )
            if u_res.get("ok"):
                reverted.append(u_res.get("receipt") or "Reverted inventory movement")
            else:
                errors.append(str(u_res.get("error") or "Inventory undo failed"))
    return reverted, errors


def _parse_lines(slots: dict[str, Any]) -> list[dict[str, Any]]:
    raw = slots.get("inventory_lines_json")
    if not raw:
        return []
    try:
        loaded = json.loads(str(raw)) if isinstance(raw, str) else raw
        return [dict(x) for x in loaded if isinstance(x, dict)] if isinstance(loaded, list) else []
    except (TypeError, ValueError):
        return []
