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
                "confirm_separate_duplicate_entry": {
                    "type": "boolean",
                    "description": "True ONLY if user explicitly asked for two identical-amount entries on the same account in one message",
                },
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

_ACTION_META_KEYS = frozenset(
    {
        "goal_id",
        "draft_id",
        "heard_transcript",
        "allow_missing_clarification",
        "confirm_separate_duplicate_entry",
    }
)

for _t in ACTION_TOOLS_SCHEMA:
    _props = _t["parameters"]["properties"]
    _props.setdefault(
        "draft_id",
        {"type": "string", "description": "Draft handle ('D1', 'D2') from earlier in this turn to update in-place"},
    )
    _props.setdefault(
        "heard_transcript",
        {"type": "string", "description": "Exact Hinglish voice transcript when audio is attached"},
    )

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
    pinned_goal_id: str | None = None,
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

    effective_open_id = item.target_open_goal_id or pinned_goal_id
    if effective_open_id and _accept_id(sb, effective_open_id, "agent_goals"):
        g_rows = (
            sb.table("agent_goals")
            .select("id,collected_slots,missing_slots")
            .eq("id", effective_open_id)
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
        patch_fields = {k: v for k, v in args.items() if k not in _ACTION_META_KEYS and v is not None}
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
    allowed_fields = {
        k: v
        for k, v in args.items()
        if k != "operation_kind" and k not in _ACTION_META_KEYS and v is not None
    }
    return [
        GoalIntentItem(
            operation_kind=op,  # type: ignore[arg-type]
            summary_title=f"{op.replace('_', ' ').title()}: {args.get('full_name') or args.get('new_name') or args.get('project_id') or 'Record'}",
            **allowed_fields,
        )
    ]


_FIN_FAMILY = frozenset({"customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill"})


def _merge_goal_intent_items(base: Any, patch: Any) -> Any:
    """Merge non-empty fields from patch onto base GoalIntentItem in-place without losing prior slots."""
    base_data = base.model_dump()
    patch_data = patch.model_dump()
    for k, v in patch_data.items():
        if k == "is_ambiguous":
            base_data[k] = bool(v)
        elif k in ("choices", "clarification_question"):
            base_data[k] = v if v else ([] if k == "choices" else None)
        elif v is not None and v != [] and v != "":
            base_data[k] = v
    return type(base).model_validate(base_data)


class TurnDraftCart:
    """Stateful per-turn workspace mapping explicit handles ('D1', 'D2', ...) to GoalIntentItem drafts."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.heard_transcript: str | None = None
        self._seq: int = 0

    def has_items(self) -> bool:
        return bool(self.entries)

    def items(self) -> list[Any]:
        return [e["item"] for e in self.entries.values()]

    def clarified_keys(self) -> set[str]:
        return {goal_item_dedup_key(e["item"]) for e in self.entries.values() if e.get("clarified")}

    def upsert(
        self,
        sb: Any,
        cand: Any,
        *,
        requested_draft_id: str | None = None,
        allow_clarify: bool = False,
        confirm_separate_duplicate: bool = False,
        acc_code_to_id: dict[str, str],
        ui_context: dict[str, Any] | None,
        user_text: str | None,
        heard_transcript: str | None,
        reply_to_bot_msg_id: int | None = None,
        telegram_chat_id: int | None = None,
        pinned_goal_id: str | None = None,
    ) -> tuple[str, Any, list[str], dict[str, Any], bool]:
        from app.services.zoho.contacts import _core_name_tokens

        def _preflight(it: Any) -> tuple[list[str], dict[str, Any]]:
            m, rs = preflight_goal_item_missing_slots(
                sb,
                it,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=user_text,
                heard_transcript=heard_transcript,
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if not getattr(it, "party_name", None) and rs.get("party_name"):
                it.party_name = str(rs["party_name"])
            return m, rs

        req_did = requested_draft_id.strip().upper() if requested_draft_id and requested_draft_id.strip() else None
        if req_did and req_did in self.entries:
            merged = _merge_goal_intent_items(self.entries[req_did]["item"], cand)
            m, rs = _preflight(merged)
            self.entries[req_did] = {
                "item": merged,
                "missing": m,
                "clarified": bool(self.entries[req_did].get("clarified") or allow_clarify),
            }
            return req_did, merged, m, rs, False

        missing, resolved_slots = _preflight(cand)
        op = str(resolved_slots.get("operation_kind") or cand.operation_kind)

        # Same-turn financial duplicate guard (blocks double-entry unless distinct payees or explicitly confirmed)
        if op in _FIN_FAMILY and not confirm_separate_duplicate:
            amt = resolved_slots.get("amount_inr")
            acc = str(resolved_slots.get("payment_account_code") or "").strip().lower()
            p_new = str(resolved_slots.get("party_name") or "").strip().lower()
            toks_new = _core_name_tokens(p_new)
            for did, ent in list(self.entries.items()):
                e_it = ent["item"]
                e_op = str(e_it.operation_kind or "")
                if e_op not in _FIN_FAMILY:
                    continue
                if (op == "customer_receipt") != (e_op == "customer_receipt"):
                    continue
                same_amt = bool(amt and e_it.amount_inr and abs(float(amt) - float(e_it.amount_inr)) < 0.01)
                e_acc = str(e_it.payment_account_code or "").strip().lower()
                same_or_blank_acc = (acc and e_acc and acc == e_acc) or (not acc or not e_acc)
                if same_amt and same_or_blank_acc:
                    p_old = str(e_it.party_name or "").strip().lower()
                    toks_old = _core_name_tokens(p_old)
                    distinct_payees = bool(p_old and p_new and (toks_old != toks_new if (toks_old and toks_new) else p_old != p_new))
                    if not distinct_payees:
                        merged = _merge_goal_intent_items(e_it, cand)
                        m2, rs2 = _preflight(merged)
                        self.entries[did] = {
                            "item": merged,
                            "missing": m2,
                            "clarified": bool(ent.get("clarified") or allow_clarify),
                        }
                        return did, merged, m2, rs2, True

        # Dedup key or single incomplete same-op draft merge
        new_key = goal_item_dedup_key(cand, resolved_slots)
        for did, ent in list(self.entries.items()):
            if goal_item_dedup_key(ent["item"]) == new_key:
                merged = _merge_goal_intent_items(ent["item"], cand)
                m2, rs2 = _preflight(merged)
                self.entries[did] = {
                    "item": merged,
                    "missing": m2,
                    "clarified": bool(ent.get("clarified") or allow_clarify),
                }
                return did, merged, m2, rs2, False

        incomplete_same_op = [
            (did, ent)
            for did, ent in self.entries.items()
            if ent.get("missing") and str(ent["item"].operation_kind) == op
        ]
        if len(incomplete_same_op) == 1:
            did, ent = incomplete_same_op[0]
            p_old = str(ent["item"].party_name or "").strip().lower()
            p_new = str(resolved_slots.get("party_name") or "").strip().lower()
            if not p_old or not p_new or _core_name_tokens(p_old) == _core_name_tokens(p_new):
                merged = _merge_goal_intent_items(ent["item"], cand)
                m2, rs2 = _preflight(merged)
                self.entries[did] = {
                    "item": merged,
                    "missing": m2,
                    "clarified": bool(ent.get("clarified") or allow_clarify),
                }
                return did, merged, m2, rs2, False

        self._seq += 1
        new_did = f"D{self._seq}"
        self.entries[new_did] = {
            "item": cand,
            "missing": missing,
            "clarified": bool(allow_clarify),
        }
        return new_did, cand, missing, resolved_slots, False


async def _enrich_candidate_entities(sb: Any, cand: Any) -> None:
    """Upgrade B: Deterministically match payees & customer orders inside the action tool."""
    from app.services import zoho
    from app.services.agent_core.schemas import DisambiguationChoice
    from app.services.agent_core.slot_resolvers import _accept_id

    op = str(cand.operation_kind or "")
    if op in ("expense", "vendor_payment", "supplier_draft_bill") and cand.party_name:
        m = await zoho.match_existing_payee(sb, cand.party_name)
        if m.get("status") == "matched":
            cand.party_name = m["canonical_name"]
            cand.is_new_payee = False
            if m.get("zoho_contact_id"):
                cand.zoho_contact_id = m["zoho_contact_id"]
            if m.get("vendor_id"):
                cand.vendor_id = m["vendor_id"]
            if m.get("person_id"):
                cand.person_id = m["person_id"]
            extra = m.get("role_hint") or (f"via {m['via_alias']}" if m.get("via_alias") else "")
            if extra and extra.lower() not in (cand.notes or "").lower():
                cand.notes = f"{extra}; {cand.notes}".strip("; ") if cand.notes else extra
        elif m.get("status") == "ambiguous":
            cand.is_ambiguous = True
            cand.choices = [DisambiguationChoice(**c) for c in (m.get("choices") or [])]
            cand.clarification_question = m.get("question")
        elif m.get("status") == "new":
            cand.is_new_payee = True

    if op in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "update_order_details",
        "customer_receipt",
    ):
        has_valid_pid = bool(cand.project_id and _accept_id(sb, cand.project_id, "projects"))
        hint = (cand.project_id if (cand.project_id and not has_valid_pid) else None) or (
            cand.party_name if op == "customer_receipt" and not has_valid_pid else None
        )
        if hint:
            om = zoho.match_existing_order_or_customer(sb, hint)
            if om.get("status") == "matched":
                cand.project_id = om["project_id"]
                if om.get("customer_id"):
                    cand.customer_id = om["customer_id"]
                if op == "customer_receipt" and om.get("customer_name"):
                    cand.party_name = om["customer_name"]
                    cand.is_new_payee = False
            elif om.get("status") == "ambiguous":
                cand.is_ambiguous = True
                cand.choices = [DisambiguationChoice(**c) for c in (om.get("choices") or [])]
                cand.clarification_question = om.get("question")
        if op == "customer_receipt" and cand.party_name and cand.is_new_payee is None and sb is not None:
            c_hits = (
                sb.table("customers")
                .select("id")
                .ilike("full_name", cand.party_name.strip())
                .eq("is_active", True)
                .limit(2)
                .execute()
                .data
                or []
            )
            cand.is_new_payee = len(c_hits) == 0


async def execute_action_tool(
    sb: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    cart: TurnDraftCart | None = None,
    staged_items: list[Any] | None = None,
    fallback_items: list[Any] | None = None,
    clarified_keys: set[str] | None = None,
    acc_code_to_id: dict[str, str],
    ui_context: dict[str, Any] | None,
    user_text: str | None,
    heard_transcript: str | None,
    reply_to_bot_msg_id: int | None = None,
    telegram_chat_id: int | None = None,
    pinned_goal_id: str | None = None,
) -> str:
    """Validate, link entities, and stage an action draft into TurnDraftCart."""
    if cart is not None and arguments.get("heard_transcript"):
        ht = str(arguments["heard_transcript"]).strip()
        if ht:
            cart.heard_transcript = ht

    candidates = _build_candidates_from_tool(sb, name, arguments)
    if not candidates:
        return json.dumps({"ok": False, "staged": False, "error": "No items supplied to action tool."})

    allow_clarify = bool(arguments.get("allow_missing_clarification"))
    confirm_dup = bool(arguments.get("confirm_separate_duplicate_entry"))
    req_draft_id = arguments.get("draft_id")

    draft_ids: list[str] = []
    missing_report: list[dict[str, Any]] = []
    matched_parties: list[str] = []

    for idx, cand in enumerate(candidates):
        await _enrich_candidate_entities(sb, cand)
        if cart is not None:
            did, saved_cand, missing, _, blocked_dup = cart.upsert(
                sb,
                cand,
                requested_draft_id=req_draft_id if len(candidates) == 1 else None,
                allow_clarify=allow_clarify,
                confirm_separate_duplicate=confirm_dup,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=user_text,
                heard_transcript=(cart.heard_transcript or heard_transcript),
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if blocked_dup:
                return json.dumps(
                    {
                        "ok": False,
                        "staged": False,
                        "duplicate_of": did,
                        "instruction": (
                            f"Draft {did} already stages this exact amount & account. "
                            f"Pass draft_id='{did}' to modify it, or confirm_separate_duplicate_entry=true "
                            "ONLY if the user requested two separate identical payments."
                        ),
                    },
                    ensure_ascii=False,
                )
            draft_ids.append(did)
            if saved_cand.party_name:
                matched_parties.append(str(saved_cand.party_name))
            if missing and not saved_cand.is_ambiguous:
                missing_report.append(
                    {"index": idx, "draft_id": did, "summary": saved_cand.summary_title, "missing_slots": missing}
                )
        else:
            missing, resolved_slots = preflight_goal_item_missing_slots(
                sb,
                cand,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=user_text,
                heard_transcript=heard_transcript,
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if not getattr(cand, "party_name", None) and resolved_slots.get("party_name"):
                cand.party_name = str(resolved_slots["party_name"])
            if cand.party_name:
                matched_parties.append(str(cand.party_name))
            if missing and not cand.is_ambiguous:
                missing_report.append({"index": idx, "summary": cand.summary_title, "missing_slots": missing})

    if cart is None:
        s_items = staged_items if staged_items is not None else []
        f_items = fallback_items if fallback_items is not None else []
        c_keys = clarified_keys if clarified_keys is not None else set()
        if missing_report and not allow_clarify:
            for cand in candidates:
                key = goal_item_dedup_key(cand)
                f_items[:] = [x for x in f_items if goal_item_dedup_key(x) != key]
                f_items.append(cand)
        else:
            for cand in candidates:
                key = goal_item_dedup_key(cand)
                s_items[:] = [x for x in s_items if goal_item_dedup_key(x) != key]
                s_items.append(cand)
                if allow_clarify:
                    c_keys.add(key)

    if missing_report and not allow_clarify:
        did_hint = draft_ids[0] if len(draft_ids) == 1 else ""
        did_clause = f"with draft_id='{did_hint}' " if did_hint else ""
        return json.dumps(
            {
                "ok": False,
                "staged": False,
                **({"draft_id": did_hint} if did_hint else {}),
                "missing_slots": missing_report,
                "instruction": (
                    f"Pre-flight found missing slot(s). Re-call this tool {did_clause}with those field(s) filled "
                    "if stated or implied in the user's message, PDF/image, or CONTEXT. "
                    f"If genuinely absent from all inputs, re-call {did_clause}with "
                    "allow_missing_clarification=true and a polite clarification_question."
                ),
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ok": True,
            "staged": True,
            "draft_id": draft_ids[0] if len(draft_ids) == 1 else ",".join(draft_ids),
            "staged_count": len(candidates),
            **({"matched_party": matched_parties[0]} if matched_parties else {}),
            "missing_for_user": missing_report,
            "ref_preview": make_zoho_reference_number("staged", len(cart.entries) if cart is not None else 1),
        },
        ensure_ascii=False,
    )
