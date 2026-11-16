"""Focused action proposal tools and in-loop pre-flight validation for Gemini 4 Hydrogen."""

from __future__ import annotations

import json
from typing import Any

from app.services.goal_primitives import make_zoho_reference_number

ACTION_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "stage_new_order",
        "description": (
            "Propose a brand-new customer order / lead (optionally pre-populating furniture items from a PDF or list, "
            "initial pipeline stage such as 'execution', material stage such as 'check_stock', and project specs). "
            "Always pauses for user Confirm before writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "Customer full name (e.g. 'Arjun', 'Vikas Jain')"},
                "rough_scope": {"type": "string", "description": "Overall furniture/room scope summary"},
                "area_landmark": {"type": "string", "description": "City, locality, or site landmark (e.g. 'Jaipur', 'Civil Lines, Jaipur')"},
                "to_stage": {"type": "string", "description": "Starting pipeline stage ('lead', 'design', 'estimating', 'site_sizing_boq', 'execution')"},
                "material_stage": {"type": "string", "description": "Optional material stage ('check_stock', 'ordered_pending', 'all_in_shop')"},
                "quoted_total": {"type": "number", "description": "Deal value in INR if mentioned"},
                "promised_delivery_date": {"type": "string", "description": "Delivery date YYYY-MM-DD if mentioned"},
                "phone": {"type": "string", "description": "Customer phone number if mentioned"},
                "specs_notes": {"type": "string", "description": "Ply/laminate/fabric/hardware specs across the order"},
                "items": {
                    "type": "array",
                    "description": "Concrete furniture items to create under project_items from PDF/text",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_name": {"type": "string", "description": "Exact furniture item name"},
                            "dimensions": {"type": "string", "description": "Dimensions/sizes if specified"},
                            "specs_notes": {"type": "string", "description": "Item-specific specs/laminates/hardware"},
                        },
                        "required": ["item_name"],
                    },
                },
                "customer_id": {"type": "string", "description": "Existing customer UUID if matched via search_customers"},
                "is_ambiguous": {"type": "boolean", "description": "True only if multiple homonym customers matched"},
                "clarification_question": {"type": "string", "description": "Question to ask user only if required info is genuinely missing"},
                "allow_missing_clarification": {"type": "boolean", "description": "Set true only after confirming required details are missing from all inputs"},
            },
            "required": ["full_name", "rough_scope"],
        },
    },
    {
        "type": "function",
        "name": "upsert_order_items",
        "description": (
            "Add or update 1 or more furniture items ('Kitchen', 'Room 2 - 9'6\" Bed', etc.) on an existing order. "
            "Unambiguous moves auto-commit with Undo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Existing order project_id UUID (or exact customer name/code)"},
                "items": {
                    "type": "array",
                    "description": "Furniture items to add or transition",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_name": {"type": "string", "description": "Exact furniture item name"},
                            "item_id": {"type": "string", "description": "Existing project_items UUID if known"},
                            "to_stage": {"type": "string", "description": "Target stage ('in_shop', 'packed_ready', 'at_site', 'qc_done')"},
                            "dimensions": {"type": "string", "description": "Dimensions if specified"},
                            "specs_notes": {"type": "string", "description": "Material/hardware specs if specified"},
                            "production_mistri": {"type": "string", "description": "Shop Mistri name or UUID"},
                            "assembly_mistri": {"type": "string", "description": "Site Assembly Mistri name or UUID"},
                        },
                        "required": ["item_name"],
                    },
                },
            },
            "required": ["project_id", "items"],
        },
    },
    {
        "type": "function",
        "name": "move_order_or_material_stage",
        "description": "Move an order's pipeline stage (Lead..Completed) OR Ansh's Material Track (check_stock / ordered_pending / all_in_shop).",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Order project_id UUID or exact customer name/code"},
                "track": {"type": "string", "enum": ["pipeline", "material"]},
                "to_stage": {"type": "string", "description": "Target stage code"},
                "estimator_ids": {"type": "array", "items": {"type": "string"}, "description": "Estimator names/UUIDs when moving to estimating"},
                "sizing_mistri": {"type": "string", "description": "Sizing Mistri name/UUID when moving to site_sizing_boq"},
                "boq_scope_changed": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "required": ["project_id", "track", "to_stage"],
        },
    },
    {
        "type": "function",
        "name": "upsert_item_materials",
        "description": "Add raw materials (ply, laminate, hinges, foam, adhesive) to a furniture item OR update material statuses (needed / ordered / in_shop).",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Order project_id UUID or exact customer name"},
                "item_name": {"type": "string", "description": "Furniture item name (optional when updating status order-wide)"},
                "materials": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "material_name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "status": {"type": "string", "description": "'needed', 'ordered', or 'in_shop'"},
                        },
                        "required": ["material_name", "quantity"],
                    },
                },
                "material_status": {"type": "string", "description": "Target status ('needed', 'ordered', 'in_shop')"},
            },
            "required": ["project_id"],
        },
    },
    {
        "type": "function",
        "name": "stage_financial_entry",
        "description": "Propose a Zoho Books money entry (customer_receipt, vendor_payment, expense, float_transfer, or supplier_draft_bill). Always shows ₹ Confirm card.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry_kind": {
                    "type": "string",
                    "enum": ["customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill"],
                },
                "amount_inr": {"type": "number", "description": "Numeric INR amount"},
                "payment_account_code": {"type": "string", "description": "Account code from PAYMENT_ACCOUNTS (e.g. 'cash_galla', 'axis', 'rgb', 'float_ansh')"},
                "to_payment_account_code": {"type": "string", "description": "Destination account code for float_transfer"},
                "party_name": {"type": "string", "description": "Customer, supplier, payee, or employee name"},
                "project_id": {"type": "string", "description": "Linked customer order UUID if applicable"},
                "expense_category": {
                    "type": "string",
                    "description": "'salary_wages'|'contractor_labor'|'raw_materials'|'transport_freight'|'food_tea_mess'|'rent'|'utilities_office'|'repairs_maintenance'|'other_expense'",
                },
                "invoice_no": {"type": "string", "description": "Supplier invoice number if bill"},
                "bill_line_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "rate": {"type": "number"},
                            "amount": {"type": "number"},
                        },
                        "required": ["description"],
                    },
                },
                "txn_date": {"type": "string", "description": "YYYY-MM-DD if specified"},
                "notes": {"type": "string"},
                "clarification_question": {"type": "string"},
                "allow_missing_clarification": {"type": "boolean"},
            },
            "required": ["entry_kind"],
        },
    },
    {
        "type": "function",
        "name": "resolve_open_goal",
        "description": "Fill missing slots on an existing open goal from `open_goals_tray` when the user answers a pending prompt or swipe-replies.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Exact goal_id UUID from open_goals_tray"},
                "area_landmark": {"type": "string"},
                "item_name": {"type": "string"},
                "rough_scope": {"type": "string"},
                "full_name": {"type": "string"},
                "amount_inr": {"type": "number"},
                "payment_account_code": {"type": "string"},
                "to_payment_account_code": {"type": "string"},
                "party_name": {"type": "string"},
                "to_stage": {"type": "string"},
                "project_id": {"type": "string"},
                "dimensions": {"type": "string"},
                "specs_notes": {"type": "string"},
            },
            "required": ["goal_id"],
        },
    },
    {
        "type": "function",
        "name": "manage_directory_or_order",
        "description": "Create/update Customers, Vendors, People/Mistris, update existing Order metadata (deal value, delivery, scope, specs), or soft-archive a record.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation_kind": {
                    "type": "string",
                    "enum": [
                        "add_customer",
                        "add_person_or_vendor",
                        "update_customer",
                        "update_vendor",
                        "update_person",
                        "update_order_details",
                        "archive_record",
                    ],
                },
                "project_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "vendor_id": {"type": "string"},
                "person_id": {"type": "string"},
                "full_name": {"type": "string"},
                "new_name": {"type": "string"},
                "person_or_vendor_kind": {"type": "string"},
                "area_landmark": {"type": "string"},
                "rough_scope": {"type": "string"},
                "phone": {"type": "string"},
                "gstin": {"type": "string"},
                "specialty": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "payout_aliases": {"type": "array", "items": {"type": "string"}},
                "role_tags": {"type": "array", "items": {"type": "string"}},
                "quoted_total": {"type": "number"},
                "promised_delivery_date": {"type": "string"},
                "boq_scope_changed": {"type": "boolean"},
                "boq_scope_note": {"type": "string"},
                "specs_notes": {"type": "string"},
                "archive_entity_kind": {"type": "string"},
                "notes": {"type": "string"},
                "allow_missing_clarification": {"type": "boolean"},
                "clarification_question": {"type": "string"},
            },
            "required": ["operation_kind"],
        },
    },
]

_ACTION_TOOL_NAMES = {str(t["name"]) for t in ACTION_TOOLS_SCHEMA}


def is_action_tool(name: str) -> bool:
    return name in _ACTION_TOOL_NAMES


def goal_item_dedup_key(it: Any, slots: dict[str, Any] | None = None) -> str:
    """Stable deduplication key matching staged action-tool items with envelope items."""
    def _v(attr: str) -> Any:
        if slots and slots.get(attr) not in (None, "", []):
            return slots[attr]
        return getattr(it, attr, None)

    if getattr(it, "target_open_goal_id", None):
        return f"open:{it.target_open_goal_id}"
    op = str(_v("operation_kind") or "")
    if _v("item_name"):
        return f"{op}:{_v('project_id') or ''}:{str(_v('item_name')).strip().lower()}"
    if op == "new_lead" and (_v("full_name") or _v("party_name")):
        who = str(_v("full_name") or _v("party_name") or "").strip().lower()
        return f"new_lead:{who}"
    if op in ("pipeline_stage_move", "material_stage_move", "update_order_details") and _v("project_id"):
        return f"{op}:{_v('project_id')}:{_v('to_stage') or ''}"
    if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill"):
        who = str(_v("party_name") or "").strip().lower()
        amt = _v("amount_inr")
        amt_s = f"{float(amt):g}" if amt not in (None, "") else ""
        return f"{op}:{who}:{amt_s}:{_v('invoice_no') or ''}"
    return f"{op}:{str(getattr(it, 'summary_title', '') or '').strip().lower()}"


def preflight_goal_item_missing_slots(
    sb: Any,
    item: Any,
    *,
    acc_code_to_id: dict[str, str],
    ui_context: dict[str, Any] | None,
    user_text: str | None,
    heard_transcript: str | None,
    reply_to_bot_msg_id: int | None = None,
    telegram_chat_id: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Run deterministic slot resolution & backfills side-effect-free to compute missing slots."""
    from app.services.agent_core.slot_resolvers import (
        _accept_id,
        _backfill_financial_slots_from_text,
        _backfill_item_slots_from_title_and_text,
        _backfill_material_slots_from_text,
        _backfill_onboarding_and_open_goal_slots,
        _compute_missing_slots_for_op,
        _map_op_to_db_goal_type,
        _sanitize_and_resolve_slot_ids,
        parse_initial_items_slot,
    )

    prior_slots: dict[str, Any] = {}
    prior_missing_slots: list[str] = []
    existing_goal_id: str | None = None

    if item.target_open_goal_id and _accept_id(sb, item.target_open_goal_id, "agent_goals"):
        g_rows = (
            sb.table("agent_goals")
            .select("id,collected_slots,missing_slots")
            .eq("id", item.target_open_goal_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if g_rows:
            existing_goal_id = g_rows[0]["id"]
            prior_slots = dict(g_rows[0].get("collected_slots") or {})
            prior_missing_slots = list(g_rows[0].get("missing_slots") or [])

    if not existing_goal_id and reply_to_bot_msg_id is not None:
        pinned_rows = (
            sb.table("agent_goals")
            .select("id,telegram_chat_id,goal_type,collected_slots,missing_slots")
            .in_("status", ["gathering_info", "needs_error_fix"])
            .eq("telegram_bot_msg_id", int(reply_to_bot_msg_id))
            .execute()
            .data
            or []
        )
        expected_gtype = _map_op_to_db_goal_type(item.operation_kind)
        matched_pinned = [
            r
            for r in pinned_rows
            if (telegram_chat_id is None or r.get("telegram_chat_id") in (None, telegram_chat_id))
            and r.get("goal_type") == expected_gtype
        ]
        if len(matched_pinned) == 1:
            g_pin = matched_pinned[0]
            existing_goal_id = g_pin["id"]
            prior_slots = dict(g_pin.get("collected_slots") or {})
            prior_missing_slots = list(g_pin.get("missing_slots") or [])

    new_slots = {
        k: v
        for k, v in item.model_dump(
            exclude={"target_open_goal_id", "is_ambiguous", "clarification_question", "choices"}
        ).items()
        if v is not None and v != [] and v != ""
    }
    slots: dict[str, Any] = {**prior_slots, **new_slots}
    if not slots.get("project_id") and (ui_context or {}).get("viewing_project_id") and item.operation_kind in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "update_order_details",
    ):
        slots["project_id"] = (ui_context or {})["viewing_project_id"]

    _backfill_financial_slots_from_text(sb, slots, user_text=user_text, heard_transcript=heard_transcript)
    _backfill_material_slots_from_text(slots, user_text=user_text, heard_transcript=heard_transcript)
    _backfill_item_slots_from_title_and_text(
        slots, summary_title=item.summary_title, user_text=user_text, heard_transcript=heard_transcript
    )
    _backfill_onboarding_and_open_goal_slots(
        slots,
        summary_title=item.summary_title,
        user_text=user_text,
        heard_transcript=heard_transcript,
        prior_missing_slots=prior_missing_slots if existing_goal_id else None,
        is_ambiguous=item.is_ambiguous,
    )
    _sanitize_and_resolve_slot_ids(sb, slots)
    if slots.get("payment_account_code") and slots["payment_account_code"] in acc_code_to_id:
        slots["payment_account_id"] = acc_code_to_id[slots["payment_account_code"]]
    if slots.get("to_payment_account_code") and slots["to_payment_account_code"] in acc_code_to_id:
        slots["to_payment_account_id"] = acc_code_to_id[slots["to_payment_account_code"]]

    op = str(slots.get("operation_kind") or item.operation_kind)
    if op in ("new_lead", "update_order_details"):
        parse_initial_items_slot(slots)
    return _compute_missing_slots_for_op(op, slots), slots


def _build_candidates_from_tool(sb: Any, name: str, args: dict[str, Any]) -> list[Any]:
    from app.services.agent_core.schemas import GoalIntentItem

    if name == "stage_new_order":
        items = args.get("items") or []
        return [
            GoalIntentItem(
                operation_kind="new_lead",
                summary_title=f"New Project: {args.get('full_name') or 'Customer'}",
                full_name=args.get("full_name"),
                area_landmark=args.get("area_landmark"),
                rough_scope=args.get("rough_scope"),
                to_stage=args.get("to_stage"),
                material_stage=args.get("material_stage"),
                quoted_total=args.get("quoted_total"),
                promised_delivery_date=args.get("promised_delivery_date"),
                phone=args.get("phone"),
                specs_notes=args.get("specs_notes"),
                customer_id=args.get("customer_id"),
                initial_items_json=json.dumps(items, ensure_ascii=False) if items else None,
                is_ambiguous=bool(args.get("is_ambiguous", False)),
                clarification_question=args.get("clarification_question"),
            )
        ]

    if name == "upsert_order_items":
        pid = args.get("project_id")
        out: list[GoalIntentItem] = []
        for entry in args.get("items") or []:
            if not isinstance(entry, dict):
                continue
            iname = entry.get("item_name")
            out.append(
                GoalIntentItem(
                    operation_kind="item_upsert_or_move",
                    summary_title=f"Add {iname}" if iname else "Update furniture item",
                    project_id=pid,
                    item_id=entry.get("item_id"),
                    item_name=iname,
                    to_stage=entry.get("to_stage") or "in_shop",
                    dimensions=entry.get("dimensions"),
                    specs_notes=entry.get("specs_notes"),
                    production_mistri_id=entry.get("production_mistri"),
                    assembly_mistri_id=entry.get("assembly_mistri"),
                )
            )
        return out

    if name == "move_order_or_material_stage":
        op = "material_stage_move" if args.get("track") == "material" else "pipeline_stage_move"
        return [
            GoalIntentItem(
                operation_kind=op,
                summary_title=f"Move {args.get('track') or 'stage'} -> {args.get('to_stage') or ''}".strip(),
                project_id=args.get("project_id"),
                to_stage=args.get("to_stage"),
                estimator_ids=args.get("estimator_ids") or [],
                sizing_mistri_id=args.get("sizing_mistri"),
                boq_scope_changed=args.get("boq_scope_changed"),
                notes=args.get("notes"),
            )
        ]

    if name == "upsert_item_materials":
        mats = args.get("materials") or []
        return [
            GoalIntentItem(
                operation_kind="item_material_upsert",
                summary_title=f"Update materials ({args.get('item_name') or 'Order'})",
                project_id=args.get("project_id"),
                item_name=args.get("item_name"),
                material_lines_json=json.dumps(mats, ensure_ascii=False) if mats else None,
                material_status=args.get("material_status"),
            )
        ]

    if name == "stage_financial_entry":
        op = str(args.get("entry_kind") or "expense")
        line_items = args.get("bill_line_items") or []
        return [
            GoalIntentItem(
                operation_kind=op,  # type: ignore[arg-type]
                summary_title=f"{op.replace('_', ' ').title()}: {args.get('party_name') or ''}".strip(": "),
                amount_inr=args.get("amount_inr"),
                payment_account_code=args.get("payment_account_code"),
                to_payment_account_code=args.get("to_payment_account_code"),
                party_name=args.get("party_name"),
                project_id=args.get("project_id"),
                expense_category=args.get("expense_category"),
                invoice_no=args.get("invoice_no"),
                bill_line_items_json=json.dumps(line_items, ensure_ascii=False) if line_items else None,
                txn_date=args.get("txn_date"),
                notes=args.get("notes"),
                clarification_question=args.get("clarification_question"),
            )
        ]

    if name == "resolve_open_goal":
        gid = str(args.get("goal_id") or "")
        op_kind = "new_lead"
        g_rows = sb.table("agent_goals").select("collected_slots").eq("id", gid).limit(1).execute().data or []
        if g_rows:
            op_kind = str((g_rows[0].get("collected_slots") or {}).get("operation_kind") or "new_lead")
        patch_fields = {k: v for k, v in args.items() if k != "goal_id" and v is not None}
        return [
            GoalIntentItem(
                target_open_goal_id=gid,
                operation_kind=op_kind,  # type: ignore[arg-type]
                summary_title=f"Update open goal {gid[:8]}",
                **patch_fields,
            )
        ]

    # manage_directory_or_order
    op = str(args.get("operation_kind") or "update_order_details")
    allowed_fields = {k: v for k, v in args.items() if k not in ("operation_kind", "allow_missing_clarification") and v is not None}
    return [
        GoalIntentItem(
            operation_kind=op,  # type: ignore[arg-type]
            summary_title=f"{op.replace('_', ' ').title()}: {args.get('full_name') or args.get('new_name') or args.get('project_id') or 'Record'}",
            **allowed_fields,
        )
    ]


def _replace_by_key(target: list[Any], item: Any) -> None:
    key = goal_item_dedup_key(item)
    target[:] = [x for x in target if goal_item_dedup_key(x) != key]
    target.append(item)


async def execute_action_tool(
    sb: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    staged_items: list[Any],
    fallback_items: list[Any],
    clarified_keys: set[str],
    acc_code_to_id: dict[str, str],
    ui_context: dict[str, Any] | None,
    user_text: str | None,
    heard_transcript: str | None,
    reply_to_bot_msg_id: int | None = None,
    telegram_chat_id: int | None = None,
) -> str:
    """Validate and stage an action proposal in-memory before final AgentCore execution."""
    candidates = _build_candidates_from_tool(sb, name, arguments)
    if not candidates:
        return json.dumps({"ok": False, "staged": False, "error": "No items supplied to action tool."})

    allow_clarify = bool(arguments.get("allow_missing_clarification"))
    missing_report: list[dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        missing, resolved_slots = preflight_goal_item_missing_slots(
            sb,
            cand,
            acc_code_to_id=acc_code_to_id,
            ui_context=ui_context,
            user_text=user_text,
            heard_transcript=heard_transcript,
            reply_to_bot_msg_id=reply_to_bot_msg_id,
            telegram_chat_id=telegram_chat_id,
        )
        if not getattr(cand, "party_name", None) and resolved_slots.get("party_name"):
            cand.party_name = str(resolved_slots["party_name"])
        if missing and not cand.is_ambiguous:
            missing_report.append(
                {
                    "index": idx,
                    "summary": cand.summary_title,
                    "missing_slots": missing,
                }
            )

    if missing_report and not allow_clarify:
        for cand in candidates:
            _replace_by_key(fallback_items, cand)
        return json.dumps(
            {
                "ok": False,
                "staged": False,
                "missing_slots": missing_report,
                "instruction": (
                    "Pre-flight validation found missing slot(s). If stated or implied in the user's "
                    "message, PDF/image, or CONTEXT, re-call the tool with those field(s) filled. "
                    "If genuinely omitted by the user, re-call with allow_missing_clarification=true "
                    "and a polite clarification_question."
                ),
            },
            ensure_ascii=False,
        )

    for cand in candidates:
        _replace_by_key(staged_items, cand)
        if allow_clarify:
            clarified_keys.add(goal_item_dedup_key(cand))

    return json.dumps(
        {
            "ok": True,
            "staged": True,
            "staged_count": len(candidates),
            "missing_for_user": missing_report,
            "ref_preview": make_zoho_reference_number("staged", len(staged_items)),
        },
        ensure_ascii=False,
    )
