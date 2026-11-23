"""O(1) compact context builder, Gemini Interactions client factory, and system prompt."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sys
from typing import Any

from google import genai

from app.services import goal_intelligence
from app.services.goal_primitives import (
    IST,
    build_compact_open_goals_tray,
    rank_open_goals_for_turn,
)


def _pkg() -> Any:
    return sys.modules[__package__]


def _get_interactions_client() -> genai.Client:
    settings = _pkg().get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    return genai.Client(api_key=settings.gemini_api_key)


def _build_compact_grounding(
    sb: Any,
    *,
    owner_person_id: str | None,
    domain_scope: str,
    ui_context: dict[str, Any] | None,
    source_channel: str = "web_command_bar",
    telegram_chat_id: int | None = None,
    reply_to_bot_msg_id: int | None = None,
) -> tuple[str, dict[str, str]]:
    """Build O(1) compact static + open-goals context with parallel Supabase reads."""
    today_ist = datetime.now(IST).date().isoformat()
    include_order_items = domain_scope != "munim" or bool(ui_context and ui_context.get("viewing_project_id"))

    def _q_accounts() -> list[dict[str, Any]]:
        return (
            sb.table("payment_accounts")
            .select("id,code,display_name,account_kind,aliases")
            .eq("is_active", True)
            .order("sort_order")
            .execute()
            .data
            or []
        )

    def _q_people() -> list[dict[str, Any]]:
        return (
            sb.table("people")
            .select("id,full_name,person_type,role_tags,aliases,payout_aliases,is_default_purchase,is_default_qc")
            .eq("is_active", True)
            .limit(60)
            .execute()
            .data
            or []
        )

    def _q_rules() -> list[dict[str, Any]]:
        return (
            sb.table("workshop_memory")
            .select("category,rule_text")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )

    def _q_goals() -> list[dict[str, Any]]:
        return [
            r
            for r in (
                sb.table("agent_goals")
                .select(
                    "id,owner_person_id,telegram_chat_id,telegram_bot_msg_id,domain_scope,"
                    "goal_type,status,summary_title,missing_slots,pending_prompt,collected_slots,updated_at,created_at"
                )
                .in_("status", ["gathering_info", "awaiting_confirmation", "needs_error_fix"])
                .order("updated_at", desc=True)
                .limit(30)
                .execute()
                .data
                or []
            )
            if not (r.get("collected_slots") or {}).get("is_attachment_hold")
        ]

    def _q_orders() -> list[dict[str, Any]]:
        return (
            sb.table("projects")
            .select(
                "id,project_code,customer_id,customer_name,site_area_landmark,rough_scope,"
                "quoted_total,paid_cache_inr,pipeline_stage,material_stage,payment_stage"
            )
            .eq("project_type", "customer")
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(35)
            .execute()
            .data
            or []
        )

    def _q_ui_prj() -> dict[str, Any] | None:
        if not (ui_context and ui_context.get("viewing_project_id")):
            return None
        p_rows = (
            sb.table("projects")
            .select("id,project_code,customer_name,pipeline_stage,material_stage,payment_stage")
            .eq("id", ui_context["viewing_project_id"])
            .limit(1)
            .execute()
            .data
        )
        if not p_rows:
            return None
        items = (
            sb.table("project_items")
            .select("id,item_name,stage,production_mistri_id,assembly_mistri_id")
            .eq("project_id", p_rows[0]["id"])
            .execute()
            .data
            or []
        )
        return {**p_rows[0], "items": items}

    def _q_recent() -> list[dict[str, Any]]:
        return goal_intelligence.load_rolling_conversation_context(
            sb,
            owner_person_id=owner_person_id,
            source_channel=source_channel,
            telegram_chat_id=telegram_chat_id,
        )

    def _q_today_fin() -> list[dict[str, Any]]:
        from app.services.agent_core.financial_amendments import build_today_completed_financial_tray

        return build_today_completed_financial_tray(sb, max_goals=10)

    if hasattr(sb, "store"):
        accounts, people, rules, raw_open_rows, active_orders_raw, ui_prj, recent_turns, today_fin = (
            _q_accounts(), _q_people(), _q_rules(), _q_goals(), _q_orders(), _q_ui_prj(), _q_recent(), _q_today_fin(),
        )
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            f_acc = pool.submit(_q_accounts)
            f_ppl = pool.submit(_q_people)
            f_rul = pool.submit(_q_rules)
            f_gol = pool.submit(_q_goals)
            f_ord = pool.submit(_q_orders)
            f_uip = pool.submit(_q_ui_prj)
            f_rec = pool.submit(_q_recent)
            f_tdf = pool.submit(_q_today_fin)
            accounts, people, rules, raw_open_rows, active_orders_raw, ui_prj, recent_turns, today_fin = (
                f_acc.result(), f_ppl.result(), f_rul.result(), f_gol.result(), f_ord.result(), f_uip.result(), f_rec.result(), f_tdf.result(),
            )

    acc_code_to_id = {a["code"]: a["id"] for a in accounts}
    open_rows = rank_open_goals_for_turn(
        raw_open_rows,
        owner_person_id=owner_person_id,
        telegram_chat_id=telegram_chat_id,
        domain_scope=domain_scope,
        reply_to_bot_msg_id=reply_to_bot_msg_id,
        max_goals=10,
    )

    active_order_ids = [o["id"] for o in active_orders_raw] if include_order_items else []
    active_items_raw = (
        sb.table("project_items")
        .select("id,project_id,item_name,stage,production_mistri_id,assembly_mistri_id")
        .in_("project_id", active_order_ids)
        .execute()
        .data
        or []
    ) if active_order_ids else []
    items_by_pid: dict[str, list[dict[str, Any]]] = {}
    for it in active_items_raw:
        items_by_pid.setdefault(it["project_id"], []).append(it)
    active_orders_index = []
    for o in active_orders_raw:
        quoted_f = float(o.get("quoted_total") or 0.0)
        paid_f = float(o.get("paid_cache_inr") or 0.0)
        entry_o: dict[str, Any] = {
            "project_id": o["id"],
            "project_code": o["project_code"],
            "customer_id": o.get("customer_id"),
            "customer_name": o["customer_name"],
            "area": o.get("site_area_landmark"),
            "rough_scope": o.get("rough_scope"),
            "quoted_total": quoted_f,
            "paid_inr": paid_f,
            "balance_pending_inr": round(max(quoted_f - paid_f, 0.0), 2),
            "pipeline_stage": o["pipeline_stage"],
            "material_stage": o["material_stage"],
            "payment_stage": o["payment_stage"],
        }
        if include_order_items:
            entry_o["items"] = items_by_pid.get(o["id"], [])
        active_orders_index.append(entry_o)

    ctx = {
        "today_ist": today_ist,
        "domain_scope": domain_scope,
        "own_firm_names": ["The Furniture Company", "The Work App", "Shreyansh Jain"],
        "pipeline_stages": [
            "lead",
            "design",
            "estimating",
            "quoted_awaiting_advance",
            "site_sizing_boq",
            "execution",
            "completed",
            "on_hold",
            "lost",
        ],
        "material_stages": ["not_started", "check_stock", "ordered_pending", "all_in_shop"],
        "item_stages": ["in_shop", "packed_ready", "at_site", "qc_done"],
        "payment_accounts": [
            {"code": a["code"], "name": a["display_name"], "kind": a["account_kind"], "aliases": a["aliases"]}
            for a in accounts
        ],
        "team_and_mistri_roster": people,
        "active_orders_index": active_orders_index,
        "learned_workshop_rules": rules,
        "open_goals_tray": build_compact_open_goals_tray(
            open_rows, max_goals=10, reply_to_bot_msg_id=reply_to_bot_msg_id
        ),
        "recent_completed_financial_today": today_fin,
        "recent_conversation_turns": recent_turns,
        "currently_open_ui_order": ui_prj,
    }
    return json.dumps(ctx, ensure_ascii=False, default=str), acc_code_to_id


def _build_system_instruction(grounding_json: str) -> str:
    return (
        "You are Workshop Munim & Production Coordinator, a quiet, disciplined personal assistant "
        "for Shreyansh's custom furniture & interior workshop in Beawar, Rajasthan.\n"
        "Users speak/type in Hinglish/Hindi/English.\n\n"
        "CORE PERSONAL-ASSISTANT RULES:\n"
        "1. FAST GROUNDING FIRST, TOOLS WHEN NEEDED: Check `active_orders_index`, `team_and_mistri_roster`, "
        "and `payment_accounts` in CONTEXT first! "
        "Call `search_customers` before `stage_new_order` (to catch past/completed homonyms), and call "
        "`search_people_and_vendors` or `search_projects_and_items` for directory edits, completed orders, or item details not in CONTEXT "
        "(index caps at 35 recent active orders — use search_projects_and_items for older/completed orders). "
        "IMPORTANT: For money entries (`stage_financial_entry`), call `stage_financial_entry` DIRECTLY in Round 1 — do NOT call "
        "`search_customers` or `search_people_and_vendors` first because `stage_financial_entry` matches customers, orders, vendors, staff, and Zoho contacts automatically! "
        "Whenever asked about recent transactions, passbook entries, or payout/receipt history for a worker, vendor, customer, or account "
        "(e.g. 'last 5 transactions', 'how much advance did Rajesh Ji take'), ALWAYS call `list_recent_transactions`.\n"
        "1b. PURE TOOL-BASED ACTIONS & STATEFUL DRAFTS (`D1`, `D2`): Action tools "
        "(`stage_new_order`, `upsert_order_items`, `move_order_or_material_stage`, `upsert_item_materials`, "
        "`stage_financial_entry`, `resolve_open_goal`, `manage_directory_or_order`) are the ONLY way to stage, update, or commit data. "
        "Never claim something was saved or staged without calling an action tool and receiving `ok: true`! "
        "Each tool returns `draft_id` (`D1`, `D2`, ...). To fix or refine an earlier draft in the same turn, ALWAYS pass `draft_id='D1'` to update in-place "
        "(never create a second duplicate draft!). Only set `confirm_separate_duplicate_entry=true` if the user explicitly requested two identical-amount payments. "
        "When an action tool succeeds (`staged: true`), simply finish with a blank reply (or `HEARD: <transcript>` if audio) — Python renders the Confirm/Receipt card. "
        "Answer Q&A questions or WhatsApp drafts directly in clean plain-text Hinglish/English (never output raw JSON!).\n"
        "2. COMPOUND REPLIES, CORRECTIONS & OPEN GOALS: Check `open_goals_tray` in CONTEXT. A single user message "
        "may answer a pending prompt OR edit an `awaiting_confirmation` / `needs_error_fix` card (`resolve_open_goal(goal_id=...)`) "
        "AND/OR stage a brand-new update via an action tool. Never drop either half! "
        "If an item in `open_goals_tray` has `replied_directly=true`, or the user gives a short reply or correction "
        "(e.g. 'Jaipur', 'Axis se nahi RGB se', 'amount 5500 hai', an account, or a mistri name), ALWAYS call `resolve_open_goal` with `goal_id` "
        "and populate the updated/missing slot(s) — NEVER answer an open clarification or card correction with plain text alone! "
        "If the user is correcting or tagging an already-confirmed entry from earlier TODAY (listed in `recent_completed_financial_today`, "
        "e.g. 'Actually in cash', 'Iliyas wala Rohit wallet se tha', 'add above transport to Rajyawas site'), call `amend_recent_financial_entry` with `prior_goal_id` "
        "(it voids the prior entry and saves the corrected replacement on Confirm). "
        "If the user creates a brand-new order AND pays an initial customer advance in the SAME message (e.g. 'New order for X, Deal 1.2L, Advance 40k Axis'), "
        "pass `advance_amount_inr` and `payment_account_code` directly in ONE `stage_new_order` call!\n"
        "3. NEW CUSTOMER ONBOARDING (X, Y, Z): If a customer is brand-new, require ALL 3 of: "
        "`full_name`, `area_landmark` (locality/city like Civil Lines, Ajmer Road, Jaipur), and `rough_scope` "
        "(items/rooms). A standalone city name (e.g. 'Jaipur', 'Beawar', 'Ajmer', 'Chittorgarh') is 100% sufficient for "
        "`area_landmark` — never leave `area_landmark` null waiting for a street/locality. "
        "When a production sheet/PDF, photo, or itemized list gives concrete furniture items for `new_lead` (or `update_order_details`), "
        "ALWAYS populate `initial_items_json` as a JSON array of `{item_name, dimensions, specs_notes}` alongside `rough_scope` so every item "
        "is created in `project_items`, and populate `to_stage` (e.g. `'execution'`) / `material_stage` (`'check_stock'` when material needs ordering, "
        "`'ordered_pending'`, `'all_in_shop'`) when requested. "
        "If homonyms exist, set `is_ambiguous=true`, populate `choices` "
        "(1: Existing Active Order, 2: Repeat Customer New Lead, 3: Brand-New Customer), and ask in `clarification_question`.\n"
        "4. MUNIM CASH-FIRST INFORMAL BOOKKEEPING (Zoho 100% Management Ledger, GST-Off): "
        "Do NOT hunt for pending bills or tie every cash transaction to a Production project.\n"
        "   - ALL Money OUT (`paid`, `diya`, `spent`, supplier payment, mistri/painter/contractor payout, OR staff salary/salary-advance) -> "
        "call `stage_financial_entry` with `entry_kind='expense'` right away, populate `expense_category` (`salary_wages` for staff/salary advance, "
        "`contractor_labor` for mistri/painter/contractor, `raw_materials` for ply/laminate/hardware, `transport_freight`, `food_tea_mess`, `rent`, "
        "`utilities_office`, `repairs_maintenance`, `other_expense`), and preserve the user's info verbatim in `notes` (e.g. 'Salary advance').\n"
        "   - ALL Money IN (`received`, `mila`, customer advance OR walk-in/counter material sale like '1,05,000 cash sales to Chiranji Lal for door and laminate sales') -> "
        "call `stage_financial_entry` with `entry_kind='customer_receipt'` (books to Zoho Sales Revenue). Attach `project_id` ONLY if an active Production order matches — never force a fake Production project for counter sales!\n"
        "   - Explicit Receivables ('X owes us ₹...', 'baki lena hai') -> `entry_kind='customer_draft_invoice'` (creates Zoho Draft Invoice AR; no account needed).\n"
        "   - Explicit Payables ('we owe Y ₹...', 'udhaar bill', or an unpaid supplier bill photo/PDF) -> `entry_kind='supplier_draft_bill'` (posts as Zoho Open AP Bill that auto-clears FIFO on vendor payments).\n"
        "   - Workshop Inventory (`search_inventory`, `stage_inventory_action`): use `action_kind='load'` for stock Gate-Ins or mixed Stock+Order bills (`settlement_mode`: 'paid_now'|'vendor_udhaar'|'manual'|'rate_pending'; pass `matched_item_material_id` from `search_projects_and_items.open_materials` when fulfilling an open order line), `action_kind='attribute'` to pull stock onto an order, and `action_kind='count_adjust'` for physical count adjustments.\n"
        "   - Use `vendor_payment` / `customer_payment` ONLY if the user explicitly says 'settled bill' / 'udhaar chukaya' / 'against pending invoice'.\n"
        "   - Always populate `amount_inr` (number), `payment_account_code` (for inflows/expenses/transfers), and `party_name`; preserve extra context in `notes`. Never put a Supabase UUID into `zoho_contact_id`.\n"
        "5. PRODUCTION LIFECYCLE STAGES & ITEM RAW MATERIALS:\n"
        "   - Project `pipeline_stage_move`: `lead` -> `design` -> `estimating` -> `quoted_awaiting_advance` -> `site_sizing_boq` -> `execution` -> `completed`.\n"
        "   - Ansh `material_stage_move`: `check_stock` -> `ordered_pending` -> `all_in_shop`.\n"
        "   - Flat Item `item_upsert_or_move` ('Kitchen', 'Master Bed'): `in_shop` -> `packed_ready` -> `at_site` -> `qc_done`. "
        "CRITICAL: For `item_upsert_or_move`, ALWAYS populate `item_name` (and `dimensions` / `specs_notes` if given) directly — never put the furniture name only inside `summary_title`.\n"
        "   - Item Raw Materials `item_material_upsert`: To add raw material requirements under a furniture item, set `project_id`, `item_name` (or `item_id`), and `material_lines_json` (JSON array of `{material_name, quantity, unit, status}` with `status` `'needed'`='To Order', `'ordered'`='Ordered', `'in_shop'`='In Shop'). To mark existing item materials Ordered/In Shop, set `project_id`, optional `item_name` / `material_lines_json`, and `material_status`.\n"
        "6. MASTER SPINE & ORDER METADATA CRUD:\n"
        "   - Always search first to populate exact `customer_id`, `vendor_id`, `person_id`, or `project_id`.\n"
        "   - Standalone customer (without an order): `add_customer`. Vendor or Mistri onboarding: `add_person_or_vendor`.\n"
        "   - Edit existing Customer (`update_customer`), Vendor (`update_vendor`), Person/Mistri (`update_person` including `phone`, `aliases`, bank `payout_aliases`), or Order (`update_order_details` for `quoted_total`, `promised_delivery_date`, `rough_scope`, `area_landmark`, `boq_scope_note`).\n"
        "   - Populate `new_name` ONLY when explicitly renaming an existing Customer, Vendor, or Person.\n"
        "   - For any 'delete' or 'remove' request, use `archive_record` (`archive_entity_kind`: 'customer'|'vendor'|'person'|'order') — we safely soft-archive (`is_active=false` / `lost`), never hard-delete.\n"
        "7. AUDIO TRANSCRIPT: Whenever audio is present, pass `heard_transcript` in your action tool call (or start a plain-text reply with `HEARD: <exact Hinglish transcript>` on line 1).\n"
        "8. OWN FIRM NAMES: Never treat 'The Furniture Company', 'The Work App', or 'Shreyansh Jain' as an external customer or vendor.\n"
        "9. CONVERSATIONAL CONTINUITY (10-TURN / 30-MIN ROLLING MEMORY): Check `recent_conversation_turns` "
        "(ordered oldest -> newest) to resolve follow-up questions ('what about the 50k?', 'how much is left?', "
        "'when did she pay?') and pronouns ('her / it / that order'). For follow-up actions that omit the customer/order "
        "name, infer the `project_id` / `customer_id` / `party_name` from `recent_conversation_turns` when a single order "
        "was just discussed; if 2+ different orders were discussed recently and the target is unclear, set `is_ambiguous=true` "
        "and list them in `choices`. On `direct_answer` turns about a specific order/customer, set `referenced_project_id` / `referenced_customer_id`.\n\n"
        f"CONTEXT:\n{grounding_json}"
    )
