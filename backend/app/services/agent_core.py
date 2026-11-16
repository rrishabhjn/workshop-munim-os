"""Omnichannel Goal-Oriented AI Engine (AgentCore) on Gemini 4 Hydrogen Interactions API.

Powers BOTH:
  - 3 Telegram Channels (Production, Munim Finance, DM) via 2.5s debounced bursts
  - Web / Mini App Persistent Command Bar + 'Needs Info' Drawer (/production & /munim)

Implements all 6 AI/Systems Hardenings:
  1. Surgical Decisions API front-door + active open-goal tray for compound sentences
  2. Canonical Postgres agent_goals checklist (previous_interaction_id strictly within-turn)
  3. Atomic compare-and-swap execution claim (claim_goal_for_execution)
  4. Quiet PA Contract: consolidated slot prompts, zero unsolicited pings, zero silent expiry
  5. Zoho Standard Reporting-Tag bridge + reference_number idempotency + 400 vs 5xx split
  6. Mandatory '🎙️ Heard: "..."' echo on audio inputs
"""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Literal
import uuid

from google import genai
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.services import agent_tools, decisions_triage, goal_intelligence, step_executors, storage, zoho
from app.services.goal_primitives import (
    CheckpointedStep,
    IST,
    RepairOption,
    ThinkingLevel,
    build_compact_open_goals_tray,
    claim_goal_for_execution,
    compute_missing_customer_onboarding_slots,
    format_inr,
    format_voice_receipt_header,
    is_high_sensitivity_master_mutation,
    select_hydrogen_thinking_level,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured Schemas Returned by Gemini 4 Hydrogen (JSON Schema Inline-Safe)
# ---------------------------------------------------------------------------
_NULL_STRINGS = {"", "null", "none", "nil", "n/a", "na", "undefined", "unknown", "tbd"}
_LIST_FIELDS = {"aliases", "payout_aliases", "role_tags", "estimator_ids", "choices", "repair_options", "goal_items"}
_FLOAT_FIELDS = {"amount_inr", "quoted_total"}
_JSON_STRING_FIELDS = {"material_lines_json", "bill_line_items_json", "slot_patch_json"}


def _strip_json_fences(raw_text: str) -> str:
    txt = (raw_text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _coerce_optional_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    txt = str(val).strip()
    if not txt or txt.lower() in _NULL_STRINGS:
        return None
    cleaned = txt.replace(",", "")
    m_lakh = re.match(r"^(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)\b", cleaned, re.IGNORECASE)
    if m_lakh:
        return round(float(m_lakh.group(1)) * 100000.0, 2)
    m_k = re.match(r"^(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*k\b", cleaned, re.IGNORECASE)
    if m_k:
        return round(float(m_k.group(1)) * 1000.0, 2)
    m_num = re.search(r"(-?\d+(?:\.\d+)?)", cleaned)
    if m_num:
        try:
            return float(m_num.group(1))
        except ValueError:
            return None
    return None


def _coerce_optional_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if val is None:
        return None
    low = str(val).strip().lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    return None


def _sanitize_gemini_nulls(raw: Any) -> Any:
    """Normalize literal 'null', formatted numbers/dates/lists, and fences in LLM JSON outputs."""
    if not isinstance(raw, dict):
        return raw
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _FLOAT_FIELDS:
            cleaned[k] = _coerce_optional_float(v)
            continue
        if k == "is_ambiguous":
            cleaned[k] = bool(_coerce_optional_bool(v))
            continue
        if k == "boq_scope_changed":
            cleaned[k] = _coerce_optional_bool(v)
            continue
        if k in _JSON_STRING_FIELDS:
            if isinstance(v, (list, dict)):
                cleaned[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, str):
                unwrapped = _strip_json_fences(v)
                if unwrapped.lower() in _NULL_STRINGS:
                    cleaned[k] = "{}" if k == "slot_patch_json" else None
                else:
                    cleaned[k] = unwrapped
            else:
                cleaned[k] = "{}" if k == "slot_patch_json" else None
            continue
        if k in _LIST_FIELDS:
            if v is None:
                cleaned[k] = []
            elif isinstance(v, str):
                if v.strip().lower() in _NULL_STRINGS:
                    cleaned[k] = []
                else:
                    cleaned[k] = [
                        part.strip()
                        for part in v.split(",")
                        if part.strip() and part.strip().lower() not in _NULL_STRINGS
                    ]
            elif isinstance(v, list):
                cleaned[k] = [
                    item
                    for item in v
                    if item is not None
                    and not (isinstance(item, str) and item.strip().lower() in _NULL_STRINGS)
                ]
            else:
                cleaned[k] = []
            continue

        if isinstance(v, str) and v.strip().lower() in _NULL_STRINGS:
            cleaned[k] = None
        elif isinstance(v, list):
            cleaned[k] = [
                item
                for item in v
                if item is not None
                and not (isinstance(item, str) and item.strip().lower() in _NULL_STRINGS)
            ]
        else:
            cleaned[k] = v
    return cleaned


class DisambiguationChoice(BaseModel):
    choice_id: str
    label: str = Field(description="Short button label (<42 chars), e.g. 'Active: Vikas Sharma (Civil Lines)'")
    slot_patch_json: str = Field(
        default="{}",
        description="JSON object string of slots to merge if user taps this button (e.g. '{\"project_id\":\"...\"}').",
    )

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class GoalIntentItem(BaseModel):
    target_open_goal_id: str | None = Field(
        default=None,
        description="Set if this action answers/updates one of OPEN_GOALS; null for a brand-new task.",
    )
    operation_kind: Literal[
        "new_lead",
        "add_customer",
        "update_customer",
        "update_vendor",
        "update_person",
        "update_order_details",
        "archive_record",
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "customer_receipt",
        "vendor_payment",
        "expense",
        "float_transfer",
        "supplier_draft_bill",
        "add_person_or_vendor",
    ]
    summary_title: str = Field(description="Crisp 1-line summary in Hinglish/English.")
    is_ambiguous: bool = Field(
        default=False,
        description="True if multiple homonym customers/projects/people match or target is unclear.",
    )
    clarification_question: str | None = Field(
        default=None,
        description="ONE consolidated polite Hinglish/English question asking for ALL missing/ambiguous info at once.",
    )
    choices: list[DisambiguationChoice] = Field(default_factory=list)

    # Production, Master Spine & Customer Slots
    project_id: str | None = None
    customer_id: str | None = None
    vendor_id: str | None = None
    person_id: str | None = None
    full_name: str | None = None
    new_name: str | None = Field(
        default=None,
        description="Populate ONLY when explicitly renaming an existing Customer, Vendor, or Person.",
    )
    area_landmark: str | None = None
    rough_scope: str | None = None
    phone: str | None = None
    gstin: str | None = None
    specialty: str | None = None
    aliases: list[str] = Field(default_factory=list)
    payout_aliases: list[str] = Field(
        default_factory=list,
        description="Worker proxy bank payee names (e.g. ['Ruksar Bano'] for Lala Ji).",
    )
    role_tags: list[str] = Field(default_factory=list)
    archive_entity_kind: str | None = Field(
        default=None,
        description="For archive_record ('delete'/'remove' requests): 'customer' | 'vendor' | 'person' | 'order'.",
    )
    promised_delivery_date: str | None = None
    quoted_total: float | None = None
    to_stage: str | None = None
    estimator_ids: list[str] = Field(default_factory=list)
    sizing_mistri_id: str | None = None
    boq_scope_changed: bool | None = None
    boq_scope_note: str | None = None
    item_id: str | None = None
    item_name: str | None = None
    dimensions: str | None = None
    specs_notes: str | None = None
    production_mistri_id: str | None = None
    assembly_mistri_id: str | None = None
    material_lines_json: str | None = Field(
        default=None,
        description='JSON array string of [{"material_name":"18mm HDHMR Ply","quantity":8,"unit":"sheets","status":"needed"}] for item_material_upsert.',
    )
    material_status: str | None = Field(
        default=None,
        description="Target material status ('needed' = To Order, 'ordered' = Ordered, 'in_shop' = In Shop) when updating item material status.",
    )

    # Munim Financial & Bill Slots
    amount_inr: float | None = Field(
        default=None,
        description="REQUIRED for money entries: numeric INR amount (e.g. 20000 for ₹20,000 or 50000 for 50k).",
    )
    payment_account_code: str | None = Field(
        default=None,
        description="REQUIRED for money entries: exact account code from PAYMENT_ACCOUNTS (e.g. 'cash_galla' for cash/galla, 'axis', 'rgb', 'paytm', 'float_ansh').",
    )
    to_payment_account_code: str | None = None
    party_name: str | None = Field(
        default=None,
        description="Customer, vendor, or payee name for receipts, bills, or payouts.",
    )
    zoho_contact_id: str | None = None
    txn_date: str | None = None
    invoice_no: str | None = None
    bill_line_items_json: str | None = Field(
        default=None,
        description="JSON array string of [{description, quantity, rate, amount}] for supplier_draft_bill.",
    )
    person_or_vendor_kind: str | None = Field(
        default=None,
        description="For add_person_or_vendor: 'salaried_karigar' | 'contractor_mistri' | 'supplier' | 'contractor'",
    )
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class AgentTurnEnvelope(BaseModel):
    heard_transcript: str | None = Field(
        default=None,
        description="Exact Hinglish/Hindi transcript when audio input is provided; null for text-only.",
    )
    intent_category: Literal[
        "goal_actions",
        "direct_answer",
        "draft_whatsapp_message",
    ]
    direct_reply_text: str | None = Field(
        default=None,
        description="Direct answer for Q&A, standalone WhatsApp customer draft, or brief note.",
    )
    referenced_project_id: str | None = Field(
        default=None,
        description="If answering a question about a specific order, populate its project_id UUID from CONTEXT.",
    )
    referenced_customer_id: str | None = Field(
        default=None,
        description="If answering a question about a specific customer, populate their customer_id UUID.",
    )
    goal_items: list[GoalIntentItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class ErrorRepairProposal(BaseModel):
    explanation_hinglish: str = Field(
        description="2-line plain Hinglish/English explanation of why Zoho or Supabase rejected the step."
    )
    repair_options: list[DisambiguationChoice] = Field(
        default_factory=list,
        description="1 to 2 concrete 1-tap repair options patching slots to fix the validation issue.",
    )

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class MemoryRuleOutput(BaseModel):
    rule_text: str = Field(max_length=160)
    category: Literal["entity", "account", "workflow", "style"]


def _inline_json_schema(
    model_cls: type[BaseModel], force_required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Inline Pydantic $defs, flatten nullable anyOfs, and strip unsupported keywords."""
    raw = deepcopy(model_cls.model_json_schema())
    defs = raw.pop("$defs", {}) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).split("/")[-1]
                return _resolve(deepcopy(defs.get(ref_name, {})))
            if "anyOf" in node:
                non_null = [
                    x
                    for x in node["anyOf"]
                    if not (isinstance(x, dict) and x.get("type") == "null")
                ]
                if len(non_null) == 1:
                    merged = _resolve(non_null[0])
                    if isinstance(merged, dict) and "description" in node:
                        merged.setdefault("description", node["description"])
                    return merged
            cleaned: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("title", "default"):
                    continue
                cleaned[k] = _resolve(v)
            if (
                cleaned.get("type") == "object"
                and isinstance(cleaned.get("properties"), dict)
                and "choice_id" in cleaned["properties"]
                and "slot_patch_json" in cleaned["properties"]
            ):
                cleaned["required"] = list(
                    dict.fromkeys([*(cleaned.get("required") or []), "slot_patch_json"])
                )
            return cleaned
        if isinstance(node, list):
            return [_resolve(x) for x in node]
        return node

    out = _resolve(raw)
    if force_required and isinstance(out, dict):
        out["required"] = list(
            dict.fromkeys([*(out.get("required") or []), *force_required])
        )
    return out


def _get_interactions_client() -> genai.Client:
    settings = get_settings()
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
) -> tuple[str, dict[str, str]]:
    """Build O(1) <1,200-token static + open-goals context (relying on tools for search)."""
    today_ist = datetime.now(IST).date().isoformat()
    accounts = (
        sb.table("payment_accounts")
        .select("id,code,display_name,account_kind,aliases")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    acc_code_to_id = {a["code"]: a["id"] for a in accounts}

    people = (
        sb.table("people")
        .select("id,full_name,person_type,role_tags,aliases,payout_aliases,is_default_purchase,is_default_qc")
        .eq("is_active", True)
        .limit(60)
        .execute()
        .data
        or []
    )
    rules = (
        sb.table("workshop_memory")
        .select("category,rule_text")
        .eq("is_active", True)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
        .data
        or []
    )

    open_q = (
        sb.table("agent_goals")
        .select("id,goal_type,status,summary_title,missing_slots,pending_prompt,collected_slots")
        .in_("status", ["gathering_info", "awaiting_confirmation", "needs_error_fix"])
        .order("updated_at", desc=True)
        .limit(3)
    )
    if owner_person_id:
        open_q = open_q.eq("owner_person_id", owner_person_id)
    open_rows = open_q.execute().data or []

    ui_prj = None
    if ui_context and ui_context.get("viewing_project_id"):
        p_rows = (
            sb.table("projects")
            .select("id,project_code,customer_name,pipeline_stage,material_stage,payment_stage")
            .eq("id", ui_context["viewing_project_id"])
            .limit(1)
            .execute()
            .data
        )
        if p_rows:
            items = (
                sb.table("project_items")
                .select("id,item_name,stage,production_mistri_id,assembly_mistri_id")
                .eq("project_id", p_rows[0]["id"])
                .execute()
                .data
                or []
            )
            ui_prj = {**p_rows[0], "items": items}

    active_orders_raw = (
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
    active_order_ids = [o["id"] for o in active_orders_raw]
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
        active_orders_index.append(
            {
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
                "items": items_by_pid.get(o["id"], []),
            }
        )

    recent_turns = goal_intelligence.load_rolling_conversation_context(
        sb,
        owner_person_id=owner_person_id,
        source_channel=source_channel,
        telegram_chat_id=telegram_chat_id,
    )

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
        "open_goals_tray": build_compact_open_goals_tray(open_rows),
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
        "and `payment_accounts` in CONTEXT first! When moving/updating an active order, item, or Mistri that is "
        "already unambiguously in `active_orders_index`, populate `goal_items` directly in 1 pass without extra tool calls. "
        "Call `search_customers` before creating a NEW lead/customer (to catch past/completed homonyms), and call "
        "`search_people_and_vendors` or `search_projects_and_items` for vendors, completed orders, or anything not in CONTEXT. "
        "IMPORTANT: `active_orders_index` lists only the 35 most recently updated active orders — if a customer or order name is "
        "NOT in `active_orders_index`, ALWAYS call `search_projects_and_items` (and `search_customers`) before concluding it does not exist.\n"
        "2. COMPOUND REPLIES & OPEN GOALS: Check `open_goals_tray` in CONTEXT. A single user message "
        "may answer an open goal (`target_open_goal_id` set) AND also issue a brand-new update "
        "(`target_open_goal_id` = null) in the same `goal_items` list. Never drop either half!\n"
        "3. NEW CUSTOMER ONBOARDING (X, Y, Z): If a customer is brand-new, require ALL 3 of: "
        "`full_name`, `area_landmark` (locality/city like Civil Lines, Ajmer Road, Jaipur), and `rough_scope` "
        "(items/rooms). If homonyms exist, set `is_ambiguous=true`, populate `choices` "
        "(1: Existing Active Order, 2: Repeat Customer New Lead, 3: Brand-New Customer), and ask in `clarification_question`.\n"
        "4. MUNIM FINANCIAL RULES (Zoho 100% Management Ledger, GST-Off): Every receipt, expense, "
        "vendor payment, or float transfer MUST populate the JSON fields `amount_inr` (number) AND `payment_account_code` "
        "(from `payment_accounts`, e.g., `cash_galla`, `axis`, `float_ansh`), plus `party_name`. "
        "CRITICAL: Never put the amount or account ONLY inside `summary_title` or `direct_reply_text` — always fill "
        "`amount_inr` and `payment_account_code` in `goal_items`. If account or party is truly missing, ask for ALL "
        "missing fields in ONE consolidated `clarification_question`. "
        "Never put a Supabase UUID into `zoho_contact_id`.\n"
        "5. PRODUCTION LIFECYCLE STAGES & ITEM RAW MATERIALS:\n"
        "   - Project `pipeline_stage_move`: `lead` -> `design` -> `estimating` -> `quoted_awaiting_advance` -> `site_sizing_boq` -> `execution` -> `completed`.\n"
        "   - Ansh `material_stage_move`: `check_stock` -> `ordered_pending` -> `all_in_shop`.\n"
        "   - Flat Item `item_upsert_or_move` ('Kitchen', 'Master Bed'): `in_shop` -> `packed_ready` -> `at_site` -> `qc_done`.\n"
        "   - Item Raw Materials `item_material_upsert`: To add raw material requirements under a furniture item, set `project_id`, `item_name` (or `item_id`), and `material_lines_json` (JSON array of `{material_name, quantity, unit, status}` with `status` `'needed'`='To Order', `'ordered'`='Ordered', `'in_shop'`='In Shop'). To mark existing item materials Ordered/In Shop, set `project_id`, optional `item_name` / `material_lines_json`, and `material_status`.\n"
        "6. MASTER SPINE & ORDER METADATA CRUD:\n"
        "   - Always search first to populate exact `customer_id`, `vendor_id`, `person_id`, or `project_id`.\n"
        "   - Standalone customer (without an order): `add_customer`. Vendor or Mistri onboarding: `add_person_or_vendor`.\n"
        "   - Edit existing Customer (`update_customer`), Vendor (`update_vendor`), Person/Mistri (`update_person` including `phone`, `aliases`, bank `payout_aliases`), or Order (`update_order_details` for `quoted_total`, `promised_delivery_date`, `rough_scope`, `area_landmark`, `boq_scope_note`).\n"
        "   - Populate `new_name` ONLY when explicitly renaming an existing Customer, Vendor, or Person.\n"
        "   - For any 'delete' or 'remove' request, use `archive_record` (`archive_entity_kind`: 'customer'|'vendor'|'person'|'order') — we safely soft-archive (`is_active=false` / `lost`), never hard-delete.\n"
        "7. AUDIO TRANSCRIPT: Whenever audio is present, populate `heard_transcript` faithfully.\n"
        "8. OWN FIRM NAMES: Never treat 'The Furniture Company', 'The Work App', or 'Shreyansh Jain' as an external customer or vendor.\n"
        "9. CONVERSATIONAL CONTINUITY (10-TURN / 30-MIN ROLLING MEMORY): Check `recent_conversation_turns` "
        "(ordered oldest -> newest) to resolve follow-up questions ('what about the 50k?', 'how much is left?', "
        "'when did she pay?') and pronouns ('her / it / that order'). For follow-up actions that omit the customer/order "
        "name, infer the `project_id` / `customer_id` / `party_name` from `recent_conversation_turns` when a single order "
        "was just discussed; if 2+ different orders were discussed recently and the target is unclear, set `is_ambiguous=true` "
        "and list them in `choices`. On `direct_answer` turns about a specific order/customer, set `referenced_project_id` / `referenced_customer_id`.\n\n"
        f"CONTEXT:\n{grounding_json}"
    )


async def run_agent_turn(
    sb: Any,
    *,
    owner_person_id: str | None,
    actor_label: str,
    source_channel: Literal["telegram", "web_command_bar"] = "web_command_bar",
    domain_scope: Literal["production", "munim", "shared"] = "production",
    text: str | None = None,
    audio_bytes: bytes | None = None,
    audio_mime_type: str = "audio/ogg",
    media_files: list[tuple[bytes, str]] | None = None,  # [(bytes, mime_type)]
    ui_context: dict[str, Any] | None = None,
    telegram_chat_id: int | None = None,
) -> dict[str, Any]:
    """Execute one complete omnichannel AgentCore turn and return structured UI/Telegram response."""
    settings = get_settings()
    grounding_json, acc_code_to_id = _build_compact_grounding(
        sb,
        owner_person_id=owner_person_id,
        domain_scope=domain_scope,
        ui_context=ui_context,
        source_channel=source_channel,
        telegram_chat_id=telegram_chat_id,
    )
    sys_inst = _build_system_instruction(grounding_json)

    # 1. Surgical Media Triage via gemini-decision-1 ($0 output) when photos/PDFs arrive
    thinking_level: ThinkingLevel = select_hydrogen_thinking_level(
        is_multi_item_dump=bool(text and (text.count(",") >= 2 or " aur " in text.lower()))
    )
    uploaded_gcs_paths: list[str] = []
    media_triage_notes: list[str] = []

    if media_files:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for idx, (m_bytes, m_mime) in enumerate(media_files):
            triage = await asyncio.to_thread(
                decisions_triage.triage_media_upload,
                m_bytes,
                m_mime,
                caption=text or "",
                domain_scope=domain_scope,
            )
            if triage.media_kind == "multi_txn_screenshot":
                return {
                    "kind": "direct_reply",
                    "heard_transcript": None,
                    "reply_text": "⚠️ Is screenshot mein ek se zyada transactions dikh rahe hain — kripya ek baar mein ek receipt bhejein ya text/voice mein batayein.",
                    "goals": [],
                }
            if triage.recommended_thinking == "high":
                thinking_level = "high"
            elif triage.recommended_thinking == "medium" and thinking_level == "low":
                thinking_level = "medium"

            ext = "pdf" if m_mime == "application/pdf" else "webp"
            folder = "supplier_bills" if triage.media_kind == "supplier_bill" else "projects/inbox"
            obj_path = f"{folder}/{now_ts}_{idx}.{ext}"
            try:
                if ext == "webp":
                    storage.upload_image_as_webp(obj_path, m_bytes)
                else:
                    storage.upload_bytes(obj_path, m_bytes, content_type=m_mime)
                uploaded_gcs_paths.append(obj_path)
            except Exception as exc:
                logger.warning("GCS upload skipped in local/dev mode: %s", exc)

            media_triage_notes.append(
                f"Attachment #{idx + 1}: classified={triage.media_kind}, ocr_difficulty={triage.ocr_difficulty:.2f}"
            )

    # 2. Build Interactions API Multimodal Input
    input_blocks: list[dict[str, Any]] = []
    if audio_bytes:
        input_blocks.append(
            {
                "type": "audio",
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "mime_type": audio_mime_type,
            }
        )
    for m_bytes, m_mime in media_files or []:
        block_type = "document" if m_mime == "application/pdf" else "image"
        input_blocks.append(
            {
                "type": block_type,
                "data": base64.b64encode(m_bytes).decode("ascii"),
                "mime_type": m_mime,
            }
        )
    prompt_parts = []
    if media_triage_notes:
        prompt_parts.append("\n".join(media_triage_notes))
    if text and text.strip():
        prompt_parts.append(text.strip())
    elif not prompt_parts:
        prompt_parts.append("Process the attached voice/media update accurately.")
    input_blocks.append({"type": "text", "text": "\n".join(prompt_parts)})

    client = _get_interactions_client()
    resp_format = {
        "type": "text",
        "mime_type": "application/json",
        "schema": _inline_json_schema(AgentTurnEnvelope, force_required=("goal_items",)),
    }

    # 3. Bounded Tool-Calling Loop (Max 3 rounds; previous_interaction_id strictly within-turn!)
    interaction = await asyncio.to_thread(
        client.interactions.create,
        model=settings.gemini_live_model,
        system_instruction=sys_inst,
        input=input_blocks,
        tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
        generation_config={"thinking_level": thinking_level},
        response_format=resp_format,
    )

    for _round in range(3):
        fn_calls = [
            s for s in (getattr(interaction, "steps", None) or [])
            if getattr(s, "type", None) == "function_call"
        ]
        if not fn_calls:
            break
        fn_results: list[dict[str, Any]] = []
        for fc in fn_calls:
            raw_args = getattr(fc, "arguments", None) or {}
            args_dict = raw_args if isinstance(raw_args, dict) else json.loads(str(raw_args))
            tool_out = await agent_tools.execute_read_tool(sb, fc.name, args_dict)
            fn_results.append(
                {
                    "type": "function_result",
                    "call_id": fc.id,
                    "name": fc.name,
                    "result": [{"type": "text", "text": tool_out}],
                }
            )
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=settings.gemini_live_model,
            previous_interaction_id=interaction.id,
            system_instruction=sys_inst,
            input=fn_results,
            tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
            generation_config={"thinking_level": thinking_level},
            response_format=resp_format,
        )

    raw_json = _strip_json_fences(getattr(interaction, "output_text", None) or "")
    envelope = AgentTurnEnvelope.model_validate_json(raw_json)
    if envelope.intent_category == "goal_actions" and not envelope.goal_items:
        try:
            retry_interaction = await asyncio.to_thread(
                client.interactions.create,
                model=settings.gemini_live_model,
                previous_interaction_id=getattr(interaction, "id", None),
                system_instruction=sys_inst,
                input=[
                    {
                        "type": "text",
                        "text": (
                            "You set intent_category='goal_actions' but omitted goal_items. "
                            "Populate goal_items with the concrete action(s) now, or set "
                            "intent_category='direct_answer' with an explanation in direct_reply_text."
                        ),
                    }
                ],
                generation_config={"thinking_level": thinking_level},
                response_format=resp_format,
            )
            retry_json = _strip_json_fences(getattr(retry_interaction, "output_text", None) or "")
            if retry_json:
                envelope = AgentTurnEnvelope.model_validate_json(retry_json)
        except Exception as exc:
            logger.warning("Empty goal_items retry failed safely: %s", exc)

    heard = envelope.heard_transcript if audio_bytes else None

    if envelope.intent_category in ("direct_answer", "draft_whatsapp_message") and not envelope.goal_items:
        raw_reply_txt = envelope.direct_reply_text or "Done."
        goal_intelligence.record_query_turn_in_history(
            sb,
            owner_person_id=owner_person_id,
            source_channel=source_channel,
            domain_scope=domain_scope,
            telegram_chat_id=telegram_chat_id,
            ui_context=ui_context or {},
            thinking_level=thinking_level,
            user_text=text,
            heard_transcript=heard,
            reply_text=raw_reply_txt,
            referenced_project_id=envelope.referenced_project_id,
            referenced_customer_id=envelope.referenced_customer_id,
        )
        nudge = goal_intelligence.build_side_quest_parked_goal_nudge(
            sb, owner_person_id=owner_person_id, touched_goal_ids=set()
        )
        reply_txt = f"{raw_reply_txt}\n\n{nudge['nudge_line']}" if nudge else raw_reply_txt
        return {
            "kind": envelope.intent_category,
            "heard_transcript": heard,
            "reply_text": reply_txt,
            "goals": [],
            "parked_goal_nudge": nudge,
        }

    if not envelope.goal_items:
        return {
            "kind": "unrecognized",
            "is_error": True,
            "heard_transcript": heard,
            "reply_text": (
                envelope.direct_reply_text
                or "⚠️ Could not match that command to an order or action — please check the customer/order name and try again."
            ),
            "goals": [],
            "parked_goal_nudge": None,
        }

    # 4. Process Each Goal Item Under Deterministic Risk-Tiered Autonomy
    processed_goals: list[dict[str, Any]] = []
    for item in envelope.goal_items:
        g_out = await _process_goal_intent_item(
            sb,
            item=item,
            acc_code_to_id=acc_code_to_id,
            owner_person_id=owner_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
            domain_scope=domain_scope,
            thinking_level=thinking_level,
            heard_transcript=heard,
            uploaded_gcs_paths=uploaded_gcs_paths,
            ui_context=ui_context or {},
            telegram_chat_id=telegram_chat_id,
            user_text=text,
        )
        processed_goals.append(g_out)

    parked_nudge: dict[str, Any] | None = None
    if processed_goals and all(g.get("status") == "completed" for g in processed_goals):
        touched = {
            str(g["goal_id"]) for g in processed_goals if g.get("goal_id")
        } | {
            str(it.target_open_goal_id)
            for it in envelope.goal_items
            if it.target_open_goal_id
        }
        parked_nudge = goal_intelligence.build_side_quest_parked_goal_nudge(
            sb, owner_person_id=owner_person_id, touched_goal_ids=touched
        )
        if parked_nudge:
            prev_receipt = str(processed_goals[-1].get("receipt") or "").strip()
            processed_goals[-1]["receipt"] = (
                f"{prev_receipt}\n\n{parked_nudge['nudge_line']}"
                if prev_receipt
                else parked_nudge["nudge_line"]
            )

    return {
        "kind": "goal_actions",
        "heard_transcript": heard,
        "reply_text": envelope.direct_reply_text,
        "goals": processed_goals,
        "parked_goal_nudge": parked_nudge,
    }


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
        "vendor_payment": "financial_txn",
        "expense": "financial_txn",
        "float_transfer": "float_transfer",
        "supplier_draft_bill": "supplier_bill",
        "add_person_or_vendor": "entity_onboarding",
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
    elif op == "update_order_details":
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


def _extract_inr_amount_from_text(*texts: str | None) -> float | None:
    """Deterministic fallback to extract an INR amount (e.g. ₹20,000, 50k, 1.5L) if LLM left amount_inr blank."""
    for raw in texts:
        txt = (raw or "").strip()
        if not txt:
            continue
        cleaned = re.sub(r"\bPRJ-[A-Z0-9-]+\b", " ", txt, flags=re.IGNORECASE)
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
        for m_num in re.finditer(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)", cleaned, re.IGNORECASE):
            num_str = m_num.group(1).replace(",", "")
            try:
                val = round(float(num_str), 2)
                if val >= 10:
                    return val
            except ValueError:
                continue
    return None


def _extract_payment_account_code_from_text(sb: Any, *texts: str | None) -> str | None:
    """Deterministic fallback to match payment_accounts aliases when LLM omitted payment_account_code."""
    combined = " ".join((t or "").strip().lower() for t in texts if t and t.strip())
    if not combined:
        return None
    accounts = (
        sb.table("payment_accounts")
        .select("code,display_name,aliases")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    candidates: list[tuple[str, str]] = []
    for a in accounts:
        code = str(a.get("code") or "").strip()
        if not code:
            continue
        tokens = [code.replace("_", " "), str(a.get("display_name") or ""), *(a.get("aliases") or [])]
        for tok in tokens:
            t_low = str(tok).strip().lower()
            if len(t_low) >= 2:
                candidates.append((t_low, code))
    candidates.sort(key=lambda pair: len(pair[0]), reverse=True)
    for tok, code in candidates:
        if re.search(rf"\b{re.escape(tok)}\b", combined):
            return code
    return None


def _backfill_financial_slots_from_text(
    sb: Any,
    slots: dict[str, Any],
    *,
    user_text: str | None,
    heard_transcript: str | None,
) -> None:
    """Ensure amount_inr, payment_account_code, and party_name are populated if mentioned in text/summary."""
    op = str(slots.get("operation_kind") or "")
    if op not in ("customer_receipt", "vendor_payment", "expense", "float_transfer"):
        return
    if slots.get("amount_inr") in (None, "", 0, 0.0):
        amt = _extract_inr_amount_from_text(user_text, heard_transcript, slots.get("summary_title"))
        if amt is not None:
            slots["amount_inr"] = amt
    if not slots.get("payment_account_code"):
        acc_code = _extract_payment_account_code_from_text(
            sb, user_text, heard_transcript, slots.get("summary_title")
        )
        if acc_code:
            slots["payment_account_code"] = acc_code
    if not slots.get("party_name") and slots.get("project_id") and op == "customer_receipt":
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

    # 1. project_id: accept valid UUID/fake ID, else resolve from PRJ-code or customer_name
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
        "customer_receipt",
    ):
        hint = slots.get("party_name") or slots.get("full_name")
        resolved_pid = _resolve_project_id_by_hint(sb, hint)
        if resolved_pid:
            slots["project_id"] = resolved_pid

    # 2. item_id: if LLM put item name ('Sofa') into item_id, move to item_name
    iid = slots.get("item_id")
    if iid is not None and not _accept_id(sb, iid, "project_items"):
        raw_item = str(iid).strip()
        if raw_item and not slots.get("item_name"):
            slots["item_name"] = raw_item
        slots.pop("item_id", None)

    # 3. People / Mistri UUID fields
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

    # 4. customer_id & vendor_id
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

    # 5. Canonicalize stages, material_status, and dates
    if slots.get("to_stage"):
        norm_st = step_executors.normalize_stage_code_for_op(op, str(slots["to_stage"]))
        if norm_st:
            slots["to_stage"] = norm_st
    if slots.get("material_status"):
        slots["material_status"] = step_executors.normalize_material_status(str(slots["material_status"]))
    for dcol in ("promised_delivery_date", "txn_date"):
        if dcol in slots and slots[dcol] is not None:
            clean_d = step_executors.sanitize_iso_date(slots[dcol])
            if clean_d:
                slots[dcol] = clean_d
            else:
                slots.pop(dcol, None)


_MAT_UNIT_RE = (
    r"(sheets?|pcs?|pieces?|mtrs?|meters?|sqft|sq\.?ft|rft|kg|kgs|ltrs?|liters?|litres?|box(?:es)?|sets?|rolls?)"
)


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


async def _process_goal_intent_item(
    sb: Any,
    *,
    item: GoalIntentItem,
    acc_code_to_id: dict[str, str],
    owner_person_id: str | None,
    actor_label: str,
    source_channel: str,
    domain_scope: str,
    thinking_level: ThinkingLevel,
    heard_transcript: str | None,
    uploaded_gcs_paths: list[str],
    ui_context: dict[str, Any],
    telegram_chat_id: int | None,
    user_text: str | None = None,
) -> dict[str, Any]:
    prior_slots: dict[str, Any] = {}
    prior_summary = ""
    existing_goal_id: str | None = None

    if item.target_open_goal_id and _accept_id(sb, item.target_open_goal_id, "agent_goals"):
        g_rows = (
            sb.table("agent_goals")
            .select("*")
            .eq("id", item.target_open_goal_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if g_rows:
            existing_goal_id = g_rows[0]["id"]
            prior_slots = dict(g_rows[0].get("collected_slots") or {})
            prior_summary = str(g_rows[0].get("summary_title") or "")

    # Merge non-null slots from current turn over prior_slots
    new_slots = {
        k: v
        for k, v in item.model_dump(
            exclude={"target_open_goal_id", "is_ambiguous", "clarification_question", "choices"}
        ).items()
        if v is not None and v != [] and v != ""
    }
    slots: dict[str, Any] = {**prior_slots, **new_slots}
    if slots.get("zoho_contact_id") and "-" in str(slots["zoho_contact_id"]):
        slots.pop("zoho_contact_id", None)
    if uploaded_gcs_paths and not slots.get("uploaded_gcs_paths"):
        slots["uploaded_gcs_paths"] = uploaded_gcs_paths

    if existing_goal_id and prior_slots:
        await asyncio.to_thread(
            goal_intelligence.maybe_distill_procedural_memory,
            sb,
            prior_summary=prior_summary,
            prior_slots=prior_slots,
            updated_slots=slots,
            user_correction_text=user_text or heard_transcript,
        )

    # Resolve UI open project fallback when unambiguous
    if not slots.get("project_id") and ui_context.get("viewing_project_id") and item.operation_kind in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "update_order_details",
    ):
        slots["project_id"] = ui_context["viewing_project_id"]
    if (
        not slots.get("project_id")
        and ui_context.get("viewing_project_id")
        and item.operation_kind == "archive_record"
        and slots.get("archive_entity_kind") == "order"
    ):
        slots["project_id"] = ui_context["viewing_project_id"]

    _backfill_financial_slots_from_text(
        sb,
        slots,
        user_text=user_text,
        heard_transcript=heard_transcript,
    )
    _backfill_material_slots_from_text(
        slots,
        user_text=user_text,
        heard_transcript=heard_transcript,
    )
    _sanitize_and_resolve_slot_ids(sb, slots)

    # Resolve payment_account_code -> payment_account_id
    if slots.get("payment_account_code") and slots["payment_account_code"] in acc_code_to_id:
        slots["payment_account_id"] = acc_code_to_id[slots["payment_account_code"]]
    if slots.get("to_payment_account_code") and slots["to_payment_account_code"] in acc_code_to_id:
        slots["to_payment_account_id"] = acc_code_to_id[slots["to_payment_account_code"]]

    op = str(slots.get("operation_kind") or item.operation_kind)

    # Compute deterministic missing slots
    missing: list[str] = []
    if op == "new_lead" and not slots.get("customer_id"):
        missing = compute_missing_customer_onboarding_slots(slots)
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
    elif op in ("customer_receipt", "vendor_payment", "expense", "float_transfer"):
        if not slots.get("amount_inr") or float(slots.get("amount_inr") or 0) <= 0:
            missing.append("amount_inr")
        if not slots.get("payment_account_id"):
            missing.append("payment_account_code")
        if op == "float_transfer" and not slots.get("to_payment_account_id"):
            missing.append("to_payment_account_code")
        if op in ("customer_receipt", "vendor_payment") and not slots.get("party_name") and not slots.get("project_id"):
            missing.append("party_name")
    elif op == "supplier_draft_bill":
        if not slots.get("party_name"):
            missing.append("party_name")
        if slots.get("amount_inr") is None:
            missing.append("amount_inr")

    choices_serialized = [c.model_dump() for c in item.choices]

    # Case A: Missing slots or Homonym Ambiguity -> status = 'gathering_info'
    if item.is_ambiguous or missing:
        prompt_q = item.clarification_question or _default_missing_question(op, missing)
        goal_row = _upsert_goal_row(
            sb,
            goal_id=existing_goal_id,
            owner_person_id=owner_person_id,
            source_channel=source_channel,
            domain_scope=domain_scope,
            telegram_chat_id=telegram_chat_id,
            ui_context=ui_context,
            goal_type=_map_op_to_db_goal_type(op),
            status="gathering_info",
            thinking_level=thinking_level,
            heard_transcript=heard_transcript,
            summary_title=item.summary_title,
            collected_slots=slots,
            missing_slots=missing,
            pending_prompt=prompt_q,
            steps=_build_steps_for_slots(op, slots),
            repair_options=choices_serialized,
            project_id=slots.get("project_id"),
            customer_id=slots.get("customer_id"),
            user_message=user_text or heard_transcript,
        )
        return {
            "goal_id": goal_row["id"],
            "status": "gathering_info",
            "summary_title": item.summary_title,
            "prompt": prompt_q,
            "choices": choices_serialized,
            "missing_slots": missing,
            "project_id": slots.get("project_id"),
            "collected_slots": slots,
        }

    # Fetch existing row for Master Spine / Order updates so sensitivity & Before->After diff are exact
    target_before_row = _fetch_target_row_for_slots(sb, op, slots)
    is_low_risk_entity_edit = op in (
        "update_customer",
        "update_vendor",
        "update_person",
        "update_order_details",
    ) and not is_high_sensitivity_master_mutation(op, slots, target_before_row)

    # Case B: Unambiguous Production Lifecycle OR Low-Sensitivity Detail Edit -> AUTO-COMMIT + 1-Tap [↩️ Undo]!
    if op in ("pipeline_stage_move", "material_stage_move", "item_upsert_or_move", "item_material_upsert") or is_low_risk_entity_edit:
        steps = _build_steps_for_slots(op, slots)
        goal_row = _upsert_goal_row(
            sb,
            goal_id=existing_goal_id,
            owner_person_id=owner_person_id,
            source_channel=source_channel,
            domain_scope="production" if op in ("pipeline_stage_move", "material_stage_move", "item_upsert_or_move", "item_material_upsert", "update_order_details") else domain_scope,
            telegram_chat_id=telegram_chat_id,
            ui_context=ui_context,
            goal_type=_map_op_to_db_goal_type(op),
            status="awaiting_confirmation",
            thinking_level=thinking_level,
            heard_transcript=heard_transcript,
            summary_title=item.summary_title,
            collected_slots=slots,
            missing_slots=[],
            pending_prompt=None,
            steps=steps,
            repair_options=[],
            project_id=slots.get("project_id"),
            customer_id=slots.get("customer_id"),
            user_message=user_text or heard_transcript,
        )
        return await confirm_and_execute_goal(
            sb,
            goal_id=str(goal_row["id"]),
            actor_person_id=owner_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
        )

    # Case C: Money, Zoho Draft Bills, New Entities, Soft-Archives & High-Sensitivity Edits -> MANDATORY CONFIRM!
    confirm_card_text = _format_confirmation_summary(op, slots, target_before_row)
    steps = _build_steps_for_slots(op, slots)
    goal_row = _upsert_goal_row(
        sb,
        goal_id=existing_goal_id,
        owner_person_id=owner_person_id,
        source_channel=source_channel,
        domain_scope="munim" if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill") else domain_scope,
        telegram_chat_id=telegram_chat_id,
        ui_context=ui_context,
        goal_type=_map_op_to_db_goal_type(op),
        status="awaiting_confirmation",
        thinking_level=thinking_level,
        heard_transcript=heard_transcript,
        summary_title=item.summary_title,
        collected_slots=slots,
        missing_slots=[],
        pending_prompt=confirm_card_text,
        steps=steps,
        repair_options=[],
        project_id=slots.get("project_id"),
        customer_id=slots.get("customer_id"),
        user_message=user_text or heard_transcript,
    )
    return {
        "goal_id": goal_row["id"],
        "status": "awaiting_confirmation",
        "summary_title": item.summary_title,
        "prompt": confirm_card_text,
        "choices": [],
        "project_id": slots.get("project_id"),
        "collected_slots": slots,
    }


def _default_missing_question(op: str, missing: list[str]) -> str:
    labels = {
        "full_name": "Full Name",
        "area_landmark": "Site Area / Landmark (e.g. Civil Lines, Jaipur)",
        "rough_scope": "Rough Scope (items/rooms)",
        "payment_account_code": "Payment Account (Cash Galla / Axis / Float)",
        "to_payment_account_code": "Destination Account / Float",
        "amount_inr": "Amount (₹)",
        "party_name": "Customer / Vendor Name",
        "customer_id": "Which Customer",
        "vendor_id": "Which Vendor",
        "person_id": "Which Person / Mistri",
        "project_id": "Which Customer Order",
        "archive_entity_kind": "Record Type (Customer / Vendor / Person / Order)",
        "item_name": "Furniture Item Name (e.g. Kitchen, Bed)",
        "material_lines_json": "Raw Material Name & Quantity (e.g. 8 sheets 18mm Ply)",
        "to_stage": "Target Stage",
    }
    nice = ", ".join(labels.get(m, m) for m in missing)
    return f"Kripya yeh details batayein ({op.replace('_', ' ')}): *{nice}*"


def _format_confirmation_summary(
    op: str, slots: dict[str, Any], before_row: dict[str, Any] | None = None
) -> str:
    row = before_row or {}
    if op == "new_lead":
        q_str = f" · Est: {format_inr(slots['quoted_total'])}" if slots.get("quoted_total") else ""
        return (
            f"🆕 *Confirm New Lead*\n"
            f"• Customer: *{slots.get('full_name')}* ({slots.get('area_landmark')})\n"
            f"• Scope: _{slots.get('rough_scope')}_{q_str}"
        )
    if op == "add_customer":
        cname = slots.get("full_name") or slots.get("party_name")
        area = f" ({slots['area_landmark']})" if slots.get("area_landmark") else ""
        ph = f"\n• Phone: `{slots['phone']}`" if slots.get("phone") else ""
        return f"👤 *Confirm New Customer*\n• Name: *{cname}*{area}{ph}"
    if op == "add_person_or_vendor":
        name = slots.get("full_name") or slots.get("party_name")
        kind = slots.get("person_or_vendor_kind") or "salaried_karigar"
        ph = f"\n• Phone: `{slots['phone']}`" if slots.get("phone") else ""
        gst = f"\n• GSTIN: `{slots['gstin']}`" if slots.get("gstin") else ""
        spec = f"\n• Specialty: _{slots['specialty']}_" if slots.get("specialty") else ""
        return f"🆕 *Confirm Directory Onboarding*\n• Name: *{name}* (`{kind}`){ph}{gst}{spec}"
    if op == "archive_record":
        kind = (slots.get("archive_entity_kind") or "record").title()
        label = (
            row.get("full_name")
            or row.get("canonical_name")
            or row.get("customer_name")
            or slots.get("full_name")
            or slots.get("party_name")
            or kind
        )
        code = f" (`{row['project_code']}`)" if row.get("project_code") else ""
        action_desc = "Mark Order *Lost / Closed*" if kind.lower() == "order" else "Soft-Archive (`is_active = false`, preserves history)"
        return f"🗂️ *Confirm Archive {kind}*\n• Target: *{label}*{code}\n• Action: {action_desc}"
    if op in ("update_customer", "update_vendor", "update_person", "update_order_details"):
        label = (
            row.get("full_name")
            or row.get("canonical_name")
            or row.get("customer_name")
            or slots.get("summary_title")
            or "Record"
        )
        diffs: list[str] = []
        old_name = row.get("full_name") or row.get("canonical_name") or ""
        if slots.get("new_name") and str(slots["new_name"]).strip().lower() != str(old_name).strip().lower():
            diffs.append(f"• Name: _{old_name or '-'}_ → *{slots['new_name']}*")
        if op == "update_order_details" and slots.get("quoted_total") is not None:
            old_q = format_inr(float(row.get("quoted_total") or 0))
            new_q = format_inr(float(slots["quoted_total"]))
            diffs.append(f"• Deal Value: {old_q} → *{new_q}*")
        if slots.get("gstin"):
            diffs.append(f"• GSTIN: `{row.get('gstin') or '-'}` → *`{str(slots['gstin']).upper()}`*")
        if slots.get("payout_aliases"):
            old_pa = ", ".join(row.get("payout_aliases") or []) or "None"
            diffs.append(f"• Bank Payout Proxy: _{old_pa}_ → + *{', '.join(slots['payout_aliases'])}*")
        if slots.get("phone"):
            diffs.append(f"• Phone: `{row.get('phone') or '-'}` → `{slots['phone']}`")
        if slots.get("area_landmark"):
            old_a = row.get("area_landmark") or row.get("site_area_landmark") or "-"
            diffs.append(f"• Area: _{old_a}_ → *{slots['area_landmark']}*")
        if slots.get("promised_delivery_date"):
            diffs.append(f"• Delivery: `{row.get('promised_delivery_date') or '-'}` → *`{slots['promised_delivery_date']}`*")
        if slots.get("rough_scope"):
            diffs.append(f"• Scope: _{slots['rough_scope']}_")
        diff_body = "\n".join(diffs) if diffs else f"• {slots.get('summary_title') or op}"
        return f"✏️ *Confirm Update — {label}*\n{diff_body}"
    if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer"):
        amt = format_inr(float(slots.get("amount_inr") or 0))
        acc = (slots.get("payment_account_code") or "").upper()
        badge = "🟢 PAISA AAYA (IN)" if op == "customer_receipt" else ("🔄 TRANSFER" if op == "float_transfer" else "🔴 PAISA GAYA (OUT)")
        party = slots.get("party_name") or slots.get("to_payment_account_code") or op
        return f"{badge}: *{amt}*\n• Party/To: *{party}*\n• Account: `{acc}`\n• Note: _{slots.get('notes') or slots.get('summary_title') or '-'}_"
    if op == "supplier_draft_bill":
        amt = format_inr(float(slots.get("amount_inr") or 0))
        return (
            f"🧾 *Confirm Zoho Draft Bill*\n"
            f"• Vendor: *{slots.get('party_name')}*\n"
            f"• Invoice: `{slots.get('invoice_no') or 'Auto'}` · Total: *{amt}*\n"
            f"_(Strictly saved as Draft in Zoho Books for Clerk review)_"
        )
    return f"📝 *Confirm*: {slots.get('summary_title') or op}"


def _build_steps_for_slots(op: str, slots: dict[str, Any]) -> list[dict[str, Any]]:
    step = CheckpointedStep(
        step_index=0,
        target="zoho" if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill") else "supabase",
        operation=op,
        payload=slots,
    )
    return [step.model_dump()]


def _upsert_goal_row(
    sb: Any,
    *,
    goal_id: str | None,
    owner_person_id: str | None,
    source_channel: str,
    domain_scope: str,
    telegram_chat_id: int | None,
    ui_context: dict[str, Any],
    goal_type: str,
    status: str,
    thinking_level: str,
    heard_transcript: str | None,
    summary_title: str,
    collected_slots: dict[str, Any],
    missing_slots: list[str],
    pending_prompt: str | None,
    steps: list[dict[str, Any]],
    repair_options: list[dict[str, Any]],
    project_id: str | None,
    customer_id: str | None,
    user_message: str | None = None,
) -> dict[str, Any]:
    safe_owner = goal_intelligence._safe_uuid_for_db(sb, owner_person_id)
    safe_proj = goal_intelligence._safe_uuid_for_db(sb, project_id, project_lookup=True)
    safe_cust = goal_intelligence._safe_uuid_for_db(sb, customer_id)
    payload: dict[str, Any] = {
        "owner_person_id": safe_owner,
        "source_channel": source_channel,
        "domain_scope": domain_scope,
        "telegram_chat_id": telegram_chat_id,
        "ui_context": ui_context,
        "goal_type": goal_type,
        "status": status,
        "thinking_level_used": thinking_level,
        "heard_transcript": heard_transcript,
        "summary_title": summary_title[:180],
        "collected_slots": collected_slots,
        "missing_slots": missing_slots,
        "pending_prompt": pending_prompt,
        "steps": steps,
        "repair_options": repair_options,
        "project_id": safe_proj,
        "customer_id": safe_cust,
    }
    if user_message and user_message.strip():
        payload["turn_digest"] = [{"user": user_message.strip()[:400]}]
    if goal_id:
        res = sb.table("agent_goals").update(payload).eq("id", goal_id).execute().data
        return res[0]
    res = sb.table("agent_goals").insert(payload).execute().data
    return res[0]


# ---------------------------------------------------------------------------
# Checkpointed Execution + 400 Error Self-Healing + 1-Tap Undo / Park
# ---------------------------------------------------------------------------
async def confirm_and_execute_goal(
    sb: Any,
    *,
    goal_id: str,
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Atomically claim and execute checkpointed steps; trigger Hydrogen repair on 400 errors."""
    claimed = claim_goal_for_execution(sb, goal_id)
    if not claimed:
        existing = sb.table("agent_goals").select("id,status,summary_title").eq("id", goal_id).limit(1).execute().data
        st = existing[0]["status"] if existing else "unknown"
        return {
            "goal_id": goal_id,
            "status": st,
            "already_processed": True,
            "receipt": f"Already {st}.",
        }

    steps: list[dict[str, Any]] = list(claimed.get("steps") or [])
    slots: dict[str, Any] = _sanitize_gemini_nulls(dict(claimed.get("collected_slots") or {}))
    _sanitize_and_resolve_slot_ids(sb, slots)
    heard = claimed.get("heard_transcript")
    receipts: list[str] = []
    undo_ids: list[str] = list(claimed.get("undo_event_ids") or [])

    for idx, step in enumerate(steps):
        if step.get("status") == "succeeded":
            if step.get("result", {}).get("receipt"):
                receipts.append(step["result"]["receipt"])
            continue

        op = step.get("operation")
        res = await _run_single_step(
            sb,
            goal_id=goal_id,
            step_index=idx,
            op=str(op),
            slots=slots,
            actor_person_id=actor_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
            heard_transcript=heard,
        )

        if res.get("ok"):
            step["status"] = "succeeded"
            step["error_kind"] = "none"
            step["result"] = res
            receipts.append(res.get("receipt") or "Done")
            undo_ids.extend(res.get("undo_event_ids") or [])
            if res.get("project_id"):
                slots["project_id"] = res["project_id"]
            if res.get("customer_id"):
                slots["customer_id"] = res["customer_id"]
            continue

        # Step failed: distinguish Transient 5xx/429 vs Validation 400!
        err_kind = res.get("error_kind") or "validation_400"
        step["retry_count"] = int(step.get("retry_count") or 0) + 1
        step["error_kind"] = err_kind
        step["error_detail"] = res.get("error")

        if err_kind == "transient_5xx":
            attempt = int(step.get("retry_count") or 1)
            if attempt <= goal_intelligence.MAX_AUTO_ZOHO_RETRIES:
                step["status"] = "failed_transient"
                retry_info = goal_intelligence.enqueue_goal_retry_task(
                    goal_id=goal_id,
                    actor_person_id=actor_person_id,
                    actor_label=actor_label,
                    attempt=attempt,
                )
                meta_retry = {
                    **(claimed.get("metadata") or {}),
                    "auto_retry_attempt": attempt,
                    "auto_retry_queued": bool(retry_info.get("queued")),
                }
                pending_msg = (
                    f"⏳ Zoho/network busy — auto-retry {attempt}/"
                    f"{goal_intelligence.MAX_AUTO_ZOHO_RETRIES} scheduled (idempotency protected)."
                )
                sb.table("agent_goals").update(
                    {
                        "status": "awaiting_confirmation",
                        "steps": steps,
                        "metadata": meta_retry,
                        "pending_prompt": pending_msg,
                    }
                ).eq("id", goal_id).execute()
                return {
                    "goal_id": goal_id,
                    "status": "transient_retry_pending",
                    "auto_retry_attempt": attempt,
                    "auto_retry_queued": bool(retry_info.get("queued")),
                    "summary_title": claimed.get("summary_title"),
                    "prompt": (
                        f"⏳ Zoho momentarily busy — auto-retry {attempt}/"
                        f"{goal_intelligence.MAX_AUTO_ZOHO_RETRIES} scheduled in background (safe & idempotent)."
                    ),
                    "choices": [],
                }

            step["status"] = "failed_transient"
            exhausted_msg = (
                "⚠️ Zoho remained unreachable after 3 automatic retries — "
                "tap Confirm later or park for Clerk."
            )
            sb.table("agent_goals").update(
                {
                    "status": "needs_error_fix",
                    "steps": steps,
                    "pending_prompt": exhausted_msg,
                    "repair_options": [],
                }
            ).eq("id", goal_id).execute()
            return {
                "goal_id": goal_id,
                "status": "needs_error_fix",
                "summary_title": claimed.get("summary_title"),
                "prompt": exhausted_msg,
                "choices": [],
                "can_park_for_clerk": True,
            }

        # Validation 400 -> Invoke Hydrogen ('medium' thinking) Self-Healing!
        step["status"] = "failed_validation"
        diagnosis = await _diagnose_validation_error_with_hydrogen(
            summary_title=claimed.get("summary_title") or op,
            slots=slots,
            error_obj=res.get("error") or {},
        )
        repairs = [c.model_dump() for c in diagnosis.repair_options]
        sb.table("agent_goals").update(
            {
                "status": "needs_error_fix",
                "steps": steps,
                "pending_prompt": diagnosis.explanation_hinglish,
                "repair_options": repairs,
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "needs_error_fix",
            "summary_title": claimed.get("summary_title"),
            "prompt": diagnosis.explanation_hinglish,
            "choices": repairs,
            "can_park_for_clerk": True,
        }

    verification = goal_intelligence.verify_committed_goal_steps(
        sb, steps=steps, slots=slots
    )
    if not verification.get("verified"):
        msg = f"⚠️ Post-save verification caught a mismatch: {verification.get('mismatch')}"
        sb.table("agent_goals").update(
            {
                "status": "needs_error_fix",
                "steps": steps,
                "collected_slots": slots,
                "pending_prompt": msg,
                "repair_options": [],
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "needs_error_fix",
            "summary_title": claimed.get("summary_title"),
            "prompt": msg,
            "choices": [],
            "can_park_for_clerk": True,
            "verification": verification,
        }

    meta_done = {
        **(claimed.get("metadata") or {}),
        "verified_checks": verification.get("checks_run", 0),
    }
    sb.table("agent_goals").update(
        {
            "status": "completed",
            "steps": steps,
            "collected_slots": slots,
            "undo_event_ids": undo_ids,
            "project_id": goal_intelligence._safe_uuid_for_db(sb, slots.get("project_id"), project_lookup=True),
            "customer_id": goal_intelligence._safe_uuid_for_db(sb, slots.get("customer_id")),
            "metadata": meta_done,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", goal_id).execute()

    has_entity_undo = any(bool((s.get("result") or {}).get("entity_undo")) for s in steps)
    return {
        "goal_id": goal_id,
        "status": "completed",
        "summary_title": claimed.get("summary_title"),
        "receipt": "\n".join(receipts),
        "undo_available": len(undo_ids) > 0 or has_entity_undo,
        "undo_event_ids": undo_ids,
        "heard_transcript": heard,
        "verified": True,
        "project_id": slots.get("project_id"),
        "collected_slots": slots,
    }


async def _run_single_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    op: str,
    slots: dict[str, Any],
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str,
    heard_transcript: str | None,
) -> dict[str, Any]:
    src = "telegram" if source_channel == "telegram" else "web"
    if op == "pipeline_stage_move":
        return await step_executors.transition_project_pipeline(
            sb,
            project_id=str(slots["project_id"]),
            to_stage=str(slots["to_stage"]),
            actor_person_id=actor_person_id,
            estimator_ids=slots.get("estimator_ids") or None,
            sizing_mistri_id=slots.get("sizing_mistri_id"),
            boq_scope_changed=slots.get("boq_scope_changed"),
            source_channel=src,
            heard_transcript=heard_transcript,
            note=slots.get("notes"),
        )
    if op == "material_stage_move":
        return await step_executors.transition_material_track(
            sb,
            project_id=str(slots["project_id"]),
            to_stage=str(slots["to_stage"]),
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
            note=slots.get("notes"),
        )
    if op == "item_upsert_or_move":
        return await step_executors.upsert_or_transition_item(
            sb,
            project_id=str(slots["project_id"]),
            item_id=slots.get("item_id"),
            item_name=slots.get("item_name"),
            to_stage=str(slots.get("to_stage") or "in_shop"),
            production_mistri_id=slots.get("production_mistri_id"),
            assembly_mistri_id=slots.get("assembly_mistri_id"),
            dimensions=slots.get("dimensions"),
            specs_notes=slots.get("specs_notes"),
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
        )
    if op == "item_material_upsert":
        project_id = str(slots["project_id"])
        raw_lines_str = slots.get("material_lines_json")
        parsed_lines: list[dict[str, Any]] = []
        if raw_lines_str:
            try:
                loaded = json.loads(raw_lines_str) if isinstance(raw_lines_str, str) else raw_lines_str
                if isinstance(loaded, list):
                    parsed_lines = [dict(x) for x in loaded if isinstance(x, dict)]
                elif isinstance(loaded, dict):
                    parsed_lines = [dict(loaded)]
            except (TypeError, ValueError):
                parsed_lines = []

        target_status = slots.get("material_status")
        is_status_update_only = bool(target_status) and (
            not parsed_lines or all(x.get("quantity") in (None, "", 0) for x in parsed_lines)
        )
        if is_status_update_only:
            query = sb.table("item_materials").select("*").eq("project_id", project_id)
            item_id_filter = slots.get("item_id")
            if not item_id_filter and slots.get("item_name"):
                found_it = (
                    sb.table("project_items")
                    .select("id")
                    .eq("project_id", project_id)
                    .ilike("item_name", str(slots["item_name"]).strip())
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if found_it:
                    item_id_filter = found_it[0]["id"]
            if item_id_filter:
                query = query.eq("item_id", item_id_filter)
            existing_mats = query.execute().data or []
            name_needles = [
                str(x.get("material_name") or "").strip().lower()
                for x in parsed_lines
                if str(x.get("material_name") or "").strip()
            ]
            if name_needles and existing_mats:
                existing_mats = [
                    m
                    for m in existing_mats
                    if any(n in str(m.get("material_name") or "").lower() for n in name_needles)
                ]
            if existing_mats:
                return step_executors.update_item_materials_rows(
                    sb,
                    material_ids=[str(m["id"]) for m in existing_mats],
                    patch={"status": str(target_status)},
                    actor_person_id=actor_person_id,
                    source_channel=src,
                    heard_transcript=heard_transcript,
                )

        if target_status:
            for line in parsed_lines:
                line.setdefault("status", str(target_status))

        return await step_executors.add_item_materials_batch(
            sb,
            project_id=project_id,
            item_id=slots.get("item_id"),
            item_name=slots.get("item_name"),
            materials=parsed_lines,
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
        )
    if op == "new_lead":
        return step_executors.create_customer_and_lead(
            sb,
            full_name=str(slots.get("full_name") or ""),
            area_landmark=str(slots.get("area_landmark") or ""),
            rough_scope=str(slots.get("rough_scope") or ""),
            phone=slots.get("phone"),
            quoted_total=float(slots.get("quoted_total") or 0),
            promised_delivery_date=slots.get("promised_delivery_date"),
            existing_customer_id=slots.get("customer_id"),
            actor_person_id=actor_person_id,
        )
    if op == "add_customer":
        return step_executors.create_standalone_customer(
            sb,
            full_name=str(slots.get("full_name") or slots.get("party_name") or ""),
            area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_customer":
        return await step_executors.update_customer_record(
            sb,
            customer_id=str(slots["customer_id"]),
            full_name=slots.get("new_name"),
            area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_vendor":
        v_kind = slots.get("person_or_vendor_kind")
        return await step_executors.update_vendor_record(
            sb,
            vendor_id=str(slots["vendor_id"]),
            canonical_name=slots.get("new_name"),
            vendor_kind=v_kind if v_kind in ("supplier", "contractor") else None,
            phone=slots.get("phone"),
            gstin=slots.get("gstin"),
            specialty=slots.get("specialty"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_person":
        p_kind = slots.get("person_or_vendor_kind")
        return step_executors.update_person_record(
            sb,
            person_id=str(slots["person_id"]),
            full_name=slots.get("new_name"),
            person_type=p_kind if p_kind in ("operator", "salaried_karigar", "contractor_mistri") else None,
            role_tags=slots.get("role_tags") or None,
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            payout_aliases=slots.get("payout_aliases") or None,
        )
    if op == "update_order_details":
        return step_executors.update_order_metadata(
            sb,
            project_id=str(slots["project_id"]),
            quoted_total=float(slots["quoted_total"]) if slots.get("quoted_total") is not None else None,
            promised_delivery_date=slots.get("promised_delivery_date"),
            rough_scope=slots.get("rough_scope"),
            site_area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            boq_scope_changed=slots.get("boq_scope_changed"),
            boq_scope_note=slots.get("boq_scope_note") or slots.get("notes"),
        )
    if op == "archive_record":
        kind = str(slots.get("archive_entity_kind") or "")
        rec_id_map = {
            "customer": slots.get("customer_id"),
            "vendor": slots.get("vendor_id"),
            "person": slots.get("person_id"),
            "order": slots.get("project_id"),
        }
        rec_id = rec_id_map.get(kind)
        if not rec_id:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Missing ID for {kind} archive."}}
        return await step_executors.archive_master_or_order_record(
            sb,
            entity_kind=kind,
            record_id=str(rec_id),
            actor_person_id=actor_person_id,
            source_channel=src,
            note=slots.get("notes"),
        )
    if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer"):
        return await step_executors.execute_munim_financial_step(
            sb,
            goal_id=goal_id,
            step_index=step_index,
            entry_kind=op,
            amount_inr=float(slots.get("amount_inr") or 0),
            payment_account_id=str(slots.get("payment_account_id") or ""),
            to_payment_account_id=slots.get("to_payment_account_id"),
            party_name=str(slots.get("party_name") or ""),
            project_id=slots.get("project_id"),
            zoho_contact_id=slots.get("zoho_contact_id"),
            txn_date=slots.get("txn_date"),
            notes=str(slots.get("notes") or ""),
            actor_label=actor_label,
        )
    if op == "supplier_draft_bill":
        vendor_name = str(slots.get("party_name") or "")
        amt = float(slots.get("amount_inr") or 0)
        inv_no = slots.get("invoice_no")
        dup = await zoho.check_duplicate_bill(sb, vendor_name, inv_no, amt)
        if dup and not slots.get("force_ignore_duplicate"):
            return {
                "ok": False,
                "error_kind": "validation_400",
                "error": {
                    "code": "DUPLICATE_BILL",
                    "message": f"Bill `{inv_no}` for `{vendor_name}` ({format_inr(amt)}) already exists in {dup['source']}.",
                },
            }
        line_items: list[dict[str, Any]] = []
        if slots.get("bill_line_items_json"):
            try:
                line_items = json.loads(str(slots["bill_line_items_json"]))
            except json.JSONDecodeError:
                line_items = []
        if not line_items:
            line_items = [{"description": slots.get("notes") or "Workshop Material", "quantity": 1, "rate": amt, "amount": amt}]
        c_res = await zoho.resolve_or_create_contact(sb, vendor_name, "vendor")
        if c_res.get("status") == "error" and goal_intelligence.is_transient_zoho_error_message(
            str(c_res.get("error") or "")
        ):
            return {
                "ok": False,
                "error_kind": "transient_5xx",
                "error": {"message": c_res.get("error")},
            }
        draft_res = await zoho.create_draft_bill(
            supplier_name=c_res.get("contact_name") or vendor_name,
            invoice_no=inv_no,
            bill_date=slots.get("txn_date"),
            line_items=line_items,
            notes=str(slots.get("notes") or ""),
            vendor_id=c_res.get("contact_id"),
        )
        if draft_res.get("error"):
            err_msg = str(draft_res["error"])
            return {
                "ok": False,
                "error_kind": (
                    "transient_5xx"
                    if goal_intelligence.is_transient_zoho_error_message(err_msg)
                    else "validation_400"
                ),
                "error": {"message": err_msg},
            }
        draft_status = str(
            draft_res.get("status")
            or (draft_res.get("bill") or {}).get("status")
            or "draft"
        )
        return {
            "ok": True,
            "draft_status": draft_status,
            "receipt": f"🧾 Zoho *Draft Bill* saved: *{vendor_name}* (`{inv_no or 'Draft'}`) · *{format_inr(amt)}*",
        }
    if op == "add_person_or_vendor":
        kind = str(slots.get("person_or_vendor_kind") or "salaried_karigar")
        name = str(slots.get("full_name") or slots.get("party_name") or "").strip()
        if not name:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Name is required."}}
        if kind in ("supplier", "contractor"):
            ins = sb.table("vendors").insert(
                {
                    "canonical_name": name,
                    "vendor_kind": kind,
                    "phone": slots.get("phone"),
                    "gstin": str(slots["gstin"]).strip().upper() if slots.get("gstin") else None,
                    "specialty": slots.get("specialty"),
                    "aliases": slots.get("aliases") or [],
                    "notes": slots.get("notes"),
                }
            ).execute().data
            return {"ok": True, "vendor_id": ins[0]["id"], "receipt": f"Added Vendor *{ins[0]['canonical_name']}* ({kind})"}
        ins = sb.table("people").insert(
            {
                "full_name": name,
                "person_type": "contractor_mistri" if kind == "contractor_mistri" else "salaried_karigar",
                "role_tags": slots.get("role_tags") or ["mistri"],
                "phone": slots.get("phone"),
                "aliases": slots.get("aliases") or [],
                "payout_aliases": slots.get("payout_aliases") or [],
            }
        ).execute().data
        return {"ok": True, "person_id": ins[0]["id"], "receipt": f"Added Mistri *{ins[0]['full_name']}* to Roster"}

    return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Unsupported step '{op}'."}}


async def _diagnose_validation_error_with_hydrogen(
    *, summary_title: str, slots: dict[str, Any], error_obj: dict[str, Any]
) -> ErrorRepairProposal:
    """Use gemini-4-hydrogen ('medium' thinking) to turn a 400 error into plain Hinglish + 1-tap repairs."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return ErrorRepairProposal(
            explanation_hinglish=f"⚠️ Save ruk gaya: {error_obj.get('message', error_obj)}",
            repair_options=[],
        )
    try:
        client = _get_interactions_client()
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=settings.gemini_live_model,
            system_instruction=(
                "You diagnose Zoho Books India or Supabase data validation errors for a workshop owner. "
                "Explain clearly in 1-2 short Hinglish lines and propose 1-2 safe slot patches in repair_options."
            ),
            input=f"Goal: {summary_title}\nSlots: {json.dumps(slots, ensure_ascii=False)}\nError: {json.dumps(error_obj, ensure_ascii=False)}",
            generation_config={"thinking_level": "medium"},
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _inline_json_schema(
                    ErrorRepairProposal, force_required=("repair_options",)
                ),
            },
        )
        return ErrorRepairProposal.model_validate_json(_strip_json_fences(interaction.output_text or ""))
    except Exception as exc:
        logger.warning("Hydrogen error diagnosis fallback: %s", exc)
        return ErrorRepairProposal(
            explanation_hinglish=f"⚠️ Data check: {error_obj.get('message', str(error_obj))[:180]}",
            repair_options=[],
        )


async def apply_slot_patch_and_resume(
    sb: Any,
    *,
    goal_id: str,
    slot_patch: dict[str, Any],
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str = "web",
    correction_note: str | None = None,
) -> dict[str, Any]:
    """Apply a 1-tap disambiguation or repair button patch and advance the goal."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    prior_slots = dict(g.get("collected_slots") or {})
    merged_slots = _sanitize_gemini_nulls({**prior_slots, **slot_patch})
    _sanitize_and_resolve_slot_ids(sb, merged_slots)
    await asyncio.to_thread(
        goal_intelligence.maybe_distill_procedural_memory,
        sb,
        prior_summary=str(g.get("summary_title") or ""),
        prior_slots=prior_slots,
        updated_slots=merged_slots,
        user_correction_text=correction_note,
    )
    op = str(merged_slots.get("operation_kind") or "stage_update")
    steps = _build_steps_for_slots(op, merged_slots)

    # If resolving homonym disambiguation ('gathering_info') for a Confirm-Gated operation,
    # pause cleanly at 'awaiting_confirmation' with a Before->After preview card!
    target_before_row = _fetch_target_row_for_slots(sb, op, merged_slots)
    is_auto_commit = op in ("pipeline_stage_move", "material_stage_move", "item_upsert_or_move", "item_material_upsert") or (
        op in ("update_customer", "update_vendor", "update_person", "update_order_details")
        and not is_high_sensitivity_master_mutation(op, merged_slots, target_before_row)
    )
    if g.get("status") == "gathering_info" and not is_auto_commit:
        confirm_card_text = _format_confirmation_summary(op, merged_slots, target_before_row)
        sb.table("agent_goals").update(
            {
                "collected_slots": merged_slots,
                "missing_slots": [],
                "pending_prompt": confirm_card_text,
                "repair_options": [],
                "steps": steps,
                "status": "awaiting_confirmation",
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "awaiting_confirmation",
            "summary_title": g.get("summary_title"),
            "prompt": confirm_card_text,
            "choices": [],
            "project_id": merged_slots.get("project_id"),
            "collected_slots": merged_slots,
        }

    sb.table("agent_goals").update(
        {
            "collected_slots": merged_slots,
            "missing_slots": [],
            "steps": steps,
            "status": "awaiting_confirmation",
        }
    ).eq("id", goal_id).execute()

    return await confirm_and_execute_goal(
        sb,
        goal_id=goal_id,
        actor_person_id=actor_person_id,
        actor_label=actor_label,
        source_channel=source_channel,
    )


def edit_open_goal_slots(
    sb: Any,
    *,
    goal_id: str,
    slot_patch: dict[str, Any],
    correction_note: str | None = None,
) -> dict[str, Any]:
    """Apply an operator [✏️ Edit] change on an open goal, distill memory, and refresh card."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    prior_slots = dict(g.get("collected_slots") or {})
    merged_slots = _sanitize_gemini_nulls({**prior_slots, **slot_patch})
    _sanitize_and_resolve_slot_ids(sb, merged_slots)
    memory_res = goal_intelligence.maybe_distill_procedural_memory(
        sb,
        prior_summary=str(g.get("summary_title") or ""),
        prior_slots=prior_slots,
        updated_slots=merged_slots,
        user_correction_text=correction_note,
    )

    op = str(merged_slots.get("operation_kind") or g.get("goal_type") or "stage_update")
    steps = _build_steps_for_slots(op, merged_slots)
    target_before_row = _fetch_target_row_for_slots(sb, op, merged_slots)
    confirm_card_text = _format_confirmation_summary(op, merged_slots, target_before_row)

    sb.table("agent_goals").update(
        {
            "collected_slots": merged_slots,
            "missing_slots": [],
            "pending_prompt": confirm_card_text,
            "repair_options": [],
            "steps": steps,
            "status": "awaiting_confirmation",
        }
    ).eq("id", goal_id).execute()

    return {
        "ok": True,
        "goal_id": goal_id,
        "status": "awaiting_confirmation",
        "summary_title": g.get("summary_title"),
        "prompt": confirm_card_text,
        "choices": [],
        "project_id": merged_slots.get("project_id"),
        "collected_slots": merged_slots,
        "memory_distilled": memory_res,
    }


def undo_completed_goal(
    sb: Any, *, goal_id: str, actor_person_id: str | None = None
) -> dict[str, Any]:
    """1-tap [↩️ Undo] for auto-committed Production stage moves AND low-risk Master/Order edits."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    if g.get("status") == "undone":
        return {"ok": True, "already_undone": True, "reverted": []}
    event_ids = list(g.get("undo_event_ids") or [])
    reverted = step_executors.undo_stage_events(sb, event_ids, actor_person_id=actor_person_id)

    entity_undos = [
        (s.get("result") or {}).get("entity_undo")
        for s in (g.get("steps") or [])
        if (s.get("result") or {}).get("entity_undo")
    ]
    for item in reversed(entity_undos):
        table = item.get("table")
        rec_id = item.get("record_id")
        snap = item.get("prior_snapshot") or {}
        if table and rec_id and snap:
            sb.table(table).update(snap).eq("id", rec_id).execute()
            for prj_undo in item.get("cascaded_projects_before") or []:
                if prj_undo.get("id") and prj_undo.get("patch"):
                    sb.table("projects").update(prj_undo["patch"]).eq("id", prj_undo["id"]).execute()
            z_cid = item.get("zoho_contact_id")
            if z_cid and table in ("customers", "vendors"):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        zoho.sync_existing_zoho_contact(
                            z_cid,
                            name=snap.get("full_name") or snap.get("canonical_name"),
                            phone=snap.get("phone") or "",
                            gstin=snap.get("gstin") if table == "vendors" else None,
                            replace_notes=snap.get("notes") or "",
                        )
                    )
                except RuntimeError:
                    pass
            reverted.append(f"Reverted {table[:-1] if table.endswith('s') else table} update")

    sb.table("agent_goals").update({"status": "undone"}).eq("id", goal_id).execute()
    return {"ok": True, "reverted": reverted}


def park_goal_for_clerk(sb: Any, goal_id: str) -> dict[str, Any]:
    """1-tap [📥 Let Clerk Fix Later] -> routes goal to Needs Info queue."""
    sb.table("agent_goals").update({"status": "parked_for_clerk"}).eq("id", goal_id).execute()
    return {"ok": True, "goal_id": goal_id, "status": "parked_for_clerk"}


def discard_goal(sb: Any, goal_id: str) -> dict[str, Any]:
    """Explicit user discard (the ONLY way an unfinished goal closes besides completion)."""
    sb.table("agent_goals").update(
        {"status": "discarded", "resolved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", goal_id).execute()
    return {"ok": True, "goal_id": goal_id, "status": "discarded"}
