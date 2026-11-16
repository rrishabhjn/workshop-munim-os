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

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Literal
import uuid

from google import genai
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services import agent_tools, decisions_triage, step_executors, storage, zoho
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
    select_hydrogen_thinking_level,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured Schemas Returned by Gemini 4 Hydrogen (JSON Schema Inline-Safe)
# ---------------------------------------------------------------------------
class DisambiguationChoice(BaseModel):
    choice_id: str
    label: str = Field(description="Short button label (<42 chars), e.g. 'Active: Vikas Sharma (Civil Lines)'")
    slot_patch_json: str = Field(
        default="{}",
        description="JSON object string of slots to merge if user taps this button (e.g. '{\"project_id\":\"...\"}').",
    )


class GoalIntentItem(BaseModel):
    target_open_goal_id: str | None = Field(
        default=None,
        description="Set if this action answers/updates one of OPEN_GOALS; null for a brand-new task.",
    )
    operation_kind: Literal[
        "new_lead",
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
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

    # Production & Customer Slots
    project_id: str | None = None
    customer_id: str | None = None
    full_name: str | None = None
    area_landmark: str | None = None
    rough_scope: str | None = None
    phone: str | None = None
    promised_delivery_date: str | None = None
    quoted_total: float | None = None
    to_stage: str | None = None
    estimator_ids: list[str] = Field(default_factory=list)
    sizing_mistri_id: str | None = None
    boq_scope_changed: bool | None = None
    item_id: str | None = None
    item_name: str | None = None
    dimensions: str | None = None
    specs_notes: str | None = None
    production_mistri_id: str | None = None
    assembly_mistri_id: str | None = None

    # Munim Financial & Bill Slots
    amount_inr: float | None = None
    payment_account_code: str | None = Field(
        default=None,
        description="Account code from PAYMENT_ACCOUNTS (e.g. 'cash_galla', 'axis', 'float_ansh').",
    )
    to_payment_account_code: str | None = None
    party_name: str | None = None
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
    goal_items: list[GoalIntentItem] = Field(default_factory=list)


class ErrorRepairProposal(BaseModel):
    explanation_hinglish: str = Field(
        description="2-line plain Hinglish/English explanation of why Zoho or Supabase rejected the step."
    )
    repair_options: list[DisambiguationChoice] = Field(
        default_factory=list,
        description="1 to 2 concrete 1-tap repair options patching slots to fix the validation issue.",
    )


class MemoryRuleOutput(BaseModel):
    rule_text: str = Field(max_length=160)
    category: Literal["entity", "account", "workflow", "style"]


def _inline_json_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Inline Pydantic $defs and strip unsupported keywords for Gemini response_format."""
    raw = deepcopy(model_cls.model_json_schema())
    defs = raw.pop("$defs", {}) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).split("/")[-1]
                return _resolve(deepcopy(defs.get(ref_name, {})))
            cleaned: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("title", "default"):
                    continue
                cleaned[k] = _resolve(v)
            return cleaned
        if isinstance(node, list):
            return [_resolve(x) for x in node]
        return node

    return _resolve(raw)


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
        "learned_workshop_rules": rules,
        "open_goals_tray": build_compact_open_goals_tray(open_rows),
        "currently_open_ui_order": ui_prj,
    }
    return json.dumps(ctx, ensure_ascii=False, default=str), acc_code_to_id


def _build_system_instruction(grounding_json: str) -> str:
    return (
        "You are Workshop Munim & Production Coordinator, a quiet, disciplined personal assistant "
        "for Shreyansh's custom furniture & interior workshop in Beawar, Rajasthan.\n"
        "Users speak/type in Hinglish/Hindi/English.\n\n"
        "CORE PERSONAL-ASSISTANT RULES:\n"
        "1. USE TOOLS BEFORE ACTING: Always call `search_customers` or `search_projects_and_items` "
        "before creating leads, moving stages, or logging receipts so homonyms (e.g. 'Sharma Ji') "
        "and repeat clients are caught cleanly. Call `search_people_and_vendors` for Mistris, "
        "proxy bank aliases (e.g. 'Ruksar Bano' -> Lala Ji), or suppliers.\n"
        "2. COMPOUND REPLIES & OPEN GOALS: Check `open_goals_tray` in CONTEXT. A single user message "
        "may answer an open goal (`target_open_goal_id` set) AND also issue a brand-new update "
        "(`target_open_goal_id` = null) in the same `goal_items` list. Never drop either half!\n"
        "3. NEW CUSTOMER ONBOARDING (X, Y, Z): If a customer is brand-new, require ALL 3 of: "
        "`full_name`, `area_landmark` (locality/city like Civil Lines, Ajmer Road, Jaipur), and `rough_scope` "
        "(items/rooms). If homonyms exist, set `is_ambiguous=true`, populate `choices` "
        "(1: Existing Active Order, 2: Repeat Customer New Lead, 3: Brand-New Customer), and ask in `clarification_question`.\n"
        "4. MUNIM FINANCIAL RULES (Zoho 100% Management Ledger, GST-Off): Every receipt, expense, "
        "vendor payment, or float transfer MUST have `amount_inr` AND `payment_account_code` "
        "(from `payment_accounts`, e.g., `cash_galla`, `axis`, `float_ansh`). If account or party is "
        "missing, ask for ALL missing fields in ONE consolidated `clarification_question`.\n"
        "5. PRODUCTION LIFECYCLE STAGES:\n"
        "   - Project `pipeline_stage_move`: `lead` -> `design` -> `estimating` -> `quoted_awaiting_advance` -> `site_sizing_boq` -> `execution` -> `completed`.\n"
        "   - Ansh `material_stage_move`: `check_stock` -> `ordered_pending` -> `all_in_shop`.\n"
        "   - Flat Item `item_upsert_or_move` ('Kitchen', 'Master Bed'): `in_shop` -> `packed_ready` -> `at_site` -> `qc_done`.\n"
        "6. AUDIO TRANSCRIPT: Whenever audio is present, populate `heard_transcript` faithfully.\n"
        "7. OWN FIRM NAMES: Never treat 'The Furniture Company', 'The Work App', or 'Shreyansh Jain' as an external customer or vendor.\n\n"
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
            triage = decisions_triage.triage_media_upload(
                m_bytes, m_mime, caption=text or "", domain_scope=domain_scope
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
        "schema": _inline_json_schema(AgentTurnEnvelope),
    }

    # 3. Bounded Tool-Calling Loop (Max 3 rounds; previous_interaction_id strictly within-turn!)
    interaction = client.interactions.create(
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
        interaction = client.interactions.create(
            model=settings.gemini_live_model,
            previous_interaction_id=interaction.id,
            system_instruction=sys_inst,
            input=fn_results,
            tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
            generation_config={"thinking_level": thinking_level},
            response_format=resp_format,
        )

    raw_json = getattr(interaction, "output_text", None) or ""
    envelope = AgentTurnEnvelope.model_validate_json(raw_json)
    heard = envelope.heard_transcript if audio_bytes else None

    if envelope.intent_category in ("direct_answer", "draft_whatsapp_message") and not envelope.goal_items:
        return {
            "kind": envelope.intent_category,
            "heard_transcript": heard,
            "reply_text": envelope.direct_reply_text or "Done.",
            "goals": [],
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
        )
        processed_goals.append(g_out)

    return {
        "kind": "goal_actions",
        "heard_transcript": heard,
        "reply_text": envelope.direct_reply_text,
        "goals": processed_goals,
    }


def _map_op_to_db_goal_type(op: str) -> str:
    mapping = {
        "new_lead": "new_lead",
        "pipeline_stage_move": "stage_update",
        "material_stage_move": "stage_update",
        "item_upsert_or_move": "stage_update",
        "customer_receipt": "financial_txn",
        "vendor_payment": "financial_txn",
        "expense": "financial_txn",
        "float_transfer": "float_transfer",
        "supplier_draft_bill": "supplier_bill",
        "add_person_or_vendor": "entity_onboarding",
    }
    return mapping.get(op, "stage_update")


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
) -> dict[str, Any]:
    prior_slots: dict[str, Any] = {}
    existing_goal_id: str | None = None

    if item.target_open_goal_id:
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

    # Merge non-null slots from current turn over prior_slots
    new_slots = {
        k: v
        for k, v in item.model_dump(
            exclude={"target_open_goal_id", "is_ambiguous", "clarification_question", "choices"}
        ).items()
        if v is not None and v != [] and v != ""
    }
    slots: dict[str, Any] = {**prior_slots, **new_slots}
    if uploaded_gcs_paths and not slots.get("uploaded_gcs_paths"):
        slots["uploaded_gcs_paths"] = uploaded_gcs_paths

    # Resolve UI open project fallback when unambiguous
    if not slots.get("project_id") and ui_context.get("viewing_project_id") and item.operation_kind in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
    ):
        slots["project_id"] = ui_context["viewing_project_id"]

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
        )
        return {
            "goal_id": goal_row["id"],
            "status": "gathering_info",
            "summary_title": item.summary_title,
            "prompt": prompt_q,
            "choices": choices_serialized,
            "missing_slots": missing,
        }

    # Case B: Unambiguous Production Lifecycle Update -> AUTO-COMMIT + 1-Tap [↩️ Undo]!
    if op in ("pipeline_stage_move", "material_stage_move", "item_upsert_or_move"):
        steps = _build_steps_for_slots(op, slots)
        goal_row = _upsert_goal_row(
            sb,
            goal_id=existing_goal_id,
            owner_person_id=owner_person_id,
            source_channel=source_channel,
            domain_scope="production",
            telegram_chat_id=telegram_chat_id,
            ui_context=ui_context,
            goal_type="stage_update",
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
        )
        return await confirm_and_execute_goal(
            sb,
            goal_id=str(goal_row["id"]),
            actor_person_id=owner_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
        )

    # Case C: Money, Zoho Draft Bills, New Customers/Leads & Roster Additions -> MANDATORY CONFIRM!
    confirm_card_text = _format_confirmation_summary(op, slots)
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
    )
    return {
        "goal_id": goal_row["id"],
        "status": "awaiting_confirmation",
        "summary_title": item.summary_title,
        "prompt": confirm_card_text,
        "choices": [],
    }


def _default_missing_question(op: str, missing: list[str]) -> str:
    labels = {
        "full_name": "Customer Full Name",
        "area_landmark": "Site Area / Landmark (e.g. Civil Lines, Jaipur)",
        "rough_scope": "Rough Scope (items/rooms)",
        "payment_account_code": "Payment Account (Cash Galla / Axis / Float)",
        "to_payment_account_code": "Destination Account / Float",
        "amount_inr": "Amount (₹)",
        "party_name": "Customer / Vendor Name",
        "project_id": "Customer Order",
        "item_name": "Furniture Item Name (e.g. Kitchen, Bed)",
        "to_stage": "Target Stage",
    }
    nice = ", ".join(labels.get(m, m) for m in missing)
    return f"Kripya yeh details batayein ({op.replace('_', ' ')}): *{nice}*"


def _format_confirmation_summary(op: str, slots: dict[str, Any]) -> str:
    if op == "new_lead":
        q_str = f" · Est: {format_inr(slots['quoted_total'])}" if slots.get("quoted_total") else ""
        return (
            f"🆕 *Confirm New Lead*\n"
            f"• Customer: *{slots.get('full_name')}* ({slots.get('area_landmark')})\n"
            f"• Scope: _{slots.get('rough_scope')}_{q_str}"
        )
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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "owner_person_id": owner_person_id,
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
        "project_id": project_id,
        "customer_id": customer_id,
    }
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
    slots: dict[str, Any] = dict(claimed.get("collected_slots") or {})
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
            step["status"] = "failed_transient"
            sb.table("agent_goals").update(
                {
                    "status": "awaiting_confirmation",
                    "steps": steps,
                    "pending_prompt": "⏳ Zoho/network momentarily busy — safe to retry (idempotency protected).",
                }
            ).eq("id", goal_id).execute()
            return {
                "goal_id": goal_id,
                "status": "transient_retry_pending",
                "summary_title": claimed.get("summary_title"),
                "prompt": "⏳ Zoho momentarily busy — tap Confirm to retry safely (won't double-post).",
                "choices": [],
            }

        # Validation 400 -> Invoke Hydrogen ('medium' thinking) Self-Healing!
        step["status"] = "failed_validation"
        diagnosis = _diagnose_validation_error_with_hydrogen(
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

    sb.table("agent_goals").update(
        {
            "status": "completed",
            "steps": steps,
            "collected_slots": slots,
            "undo_event_ids": undo_ids,
            "project_id": slots.get("project_id"),
            "customer_id": slots.get("customer_id"),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", goal_id).execute()

    return {
        "goal_id": goal_id,
        "status": "completed",
        "summary_title": claimed.get("summary_title"),
        "receipt": "\n".join(receipts),
        "undo_available": len(undo_ids) > 0,
        "undo_event_ids": undo_ids,
        "heard_transcript": heard,
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
        draft_res = await zoho.create_draft_bill(
            supplier_name=c_res.get("contact_name") or vendor_name,
            invoice_no=inv_no,
            bill_date=slots.get("txn_date"),
            line_items=line_items,
            notes=str(slots.get("notes") or ""),
            vendor_id=c_res.get("contact_id"),
        )
        if draft_res.get("error"):
            return {
                "ok": False,
                "error_kind": "validation_400",
                "error": {"message": draft_res["error"]},
            }
        return {
            "ok": True,
            "receipt": f"🧾 Zoho *Draft Bill* saved: *{vendor_name}* (`{inv_no or 'Draft'}`) · *{format_inr(amt)}*",
        }
    if op == "add_person_or_vendor":
        kind = str(slots.get("person_or_vendor_kind") or "salaried_karigar")
        name = str(slots.get("full_name") or slots.get("party_name") or "").strip()
        if not name:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Name is required."}}
        if kind in ("supplier", "contractor"):
            ins = sb.table("vendors").insert({"canonical_name": name, "vendor_kind": kind, "phone": slots.get("phone"), "notes": slots.get("notes")}).execute().data
            return {"ok": True, "receipt": f"Added Vendor *{ins[0]['canonical_name']}* ({kind})"}
        ins = sb.table("people").insert({"full_name": name, "person_type": "contractor_mistri" if kind == "contractor_mistri" else "salaried_karigar", "role_tags": ["mistri"], "phone": slots.get("phone")}).execute().data
        return {"ok": True, "receipt": f"Added Mistri *{ins[0]['full_name']}* to Roster"}

    return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Unsupported step '{op}'."}}


def _diagnose_validation_error_with_hydrogen(
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
        interaction = client.interactions.create(
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
                "schema": _inline_json_schema(ErrorRepairProposal),
            },
        )
        return ErrorRepairProposal.model_validate_json(interaction.output_text or "")
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
) -> dict[str, Any]:
    """Apply a 1-tap disambiguation or repair button patch and advance the goal."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    merged_slots = {**(g.get("collected_slots") or {}), **slot_patch}
    op = str(merged_slots.get("operation_kind") or "stage_update")
    steps = _build_steps_for_slots(op, merged_slots)

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


def undo_completed_goal(
    sb: Any, *, goal_id: str, actor_person_id: str | None = None
) -> dict[str, Any]:
    """1-tap [↩️ Undo] for auto-committed Production stage updates."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    if g.get("status") == "undone":
        return {"ok": True, "already_undone": True, "reverted": []}
    event_ids = list(g.get("undo_event_ids") or [])
    reverted = step_executors.undo_stage_events(sb, event_ids, actor_person_id=actor_person_id)
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
