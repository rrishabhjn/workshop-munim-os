"""Phase 1 Core Domain & AI-Systems Hardening Primitives.

Implements the canonical state-machine models and deterministic helpers for:
  1. Surgical Decisions API routing criteria + compound active-goal tray snapshots
  2. Canonical cross-channel AgentGoal checklist (O(1) context, zero silent expiry)
  3. 2.5s Telegram burst inbox batching + atomic compare-and-swap goal execution claim
  4. Quiet Personal-Assistant contract (consolidated missing slots, never auto-expires)
  5. Deterministic Zoho Standard Plan idempotency keys (reference_number) + transient vs 400 split
  6. Mandatory '🎙️ Heard: "..."' transcript echo on voice inputs
  7. Reversible 1-tap [↩️ Undo] snapshots for Production lifecycle moves
  8. IST Worker Attendance worked-minutes & >6:00 PM overtime math
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field

IST = timezone(timedelta(hours=5, minutes=30))

# Locked Phase 1 Stage Constants
PROJECT_PIPELINE_STAGES: tuple[str, ...] = (
    "lead",
    "design",
    "estimating",
    "quoted_awaiting_advance",
    "site_sizing_boq",
    "execution",
    "completed",
    "on_hold",
    "lost",
)

MATERIAL_TRACK_STAGES: tuple[str, ...] = (
    "not_started",
    "check_stock",
    "ordered_pending",
    "all_in_shop",
)

PAYMENT_TRACK_STAGES: tuple[str, ...] = (
    "advance_pending",
    "advance_in",
    "partial_paid",
    "payment_complete",
)

ITEM_EXECUTION_STAGES: tuple[str, ...] = (
    "in_shop",
    "packed_ready",
    "at_site",
    "qc_done",
)

NEW_CUSTOMER_REQUIRED_SLOTS: tuple[str, ...] = (
    "full_name",
    "area_landmark",
    "rough_scope",
)

GoalStatus = Literal[
    "gathering_info",
    "awaiting_confirmation",
    "executing",
    "needs_error_fix",
    "parked_for_clerk",
    "completed",
    "undone",
    "discarded",
]

GoalType = Literal[
    "new_lead",
    "stage_update",
    "assign_owner",
    "financial_txn",
    "supplier_bill",
    "float_transfer",
    "salary_advance",
    "entity_onboarding",
    "query",
    "repair",
]

StepTarget = Literal["supabase", "zoho", "gcs"]
StepStatus = Literal["pending", "succeeded", "failed_transient", "failed_validation"]
StepErrorKind = Literal["none", "transient_5xx", "validation_400"]
ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]


class CheckpointedStep(BaseModel):
    """Single idempotent execution step inside an AgentGoal (Hardening #2 & #5)."""

    step_uuid: str = Field(default_factory=lambda: str(uuid.uuid4()))
    step_index: int = Field(ge=0)
    target: StepTarget
    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = "pending"
    zoho_reference_number: str | None = None
    retry_count: int = Field(default=0, ge=0)
    error_kind: StepErrorKind = "none"
    error_detail: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class RepairOption(BaseModel):
    """1-tap repair button payload proposed by Gemini on 400 validation errors."""

    option_id: str
    label: str = Field(max_length=64)
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    park_for_clerk: bool = False


class AgentGoalChecklist(BaseModel):
    """Canonical cross-channel AI goal state (Hardening #1..#6 — never silently expires)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    owner_person_id: str | None = None
    source_channel: Literal["telegram", "web_command_bar", "system"] = "telegram"
    domain_scope: Literal["production", "munim", "attendance", "shared"] = "production"
    goal_type: GoalType
    status: GoalStatus = "gathering_info"
    thinking_level_used: ThinkingLevel = "low"
    heard_transcript: str | None = None
    summary_title: str = ""
    collected_slots: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    pending_prompt: str | None = None
    steps: list[CheckpointedStep] = Field(default_factory=list)
    repair_options: list[RepairOption] = Field(default_factory=list)
    undo_event_ids: list[str] = Field(default_factory=list)
    project_id: str | None = None
    customer_id: str | None = None
    lock_version: int = 0


# ---------------------------------------------------------------------------
# Hardening #1: Surgical Decisions API Gate Helper + Active-Goal Tray Builder
# ---------------------------------------------------------------------------
def should_run_decisions_front_door(
    *,
    is_group_chat: bool,
    is_bot_mentioned_or_reply: bool,
    has_photo_or_pdf: bool,
    domain_channel: str,
) -> bool:
    """Run gemini-decision-1 ONLY when it prevents waking Hydrogen or triages raw media."""
    if domain_channel == "attendance":
        return has_photo_or_pdf
    if has_photo_or_pdf:
        return True
    if is_group_chat and not is_bot_mentioned_or_reply:
        return True
    return False


_COMPLEX_PUSHBACK_RE = re.compile(
    r"\b(?:why|confus\w*|what are you|these are|already|pehle hi|galat|wrong|not expense|nahi)\b",
    re.IGNORECASE,
)


def has_complex_turn_cues(
    text: str | None,
    *,
    has_pdf: bool = False,
    open_goals_count: int = 0,
) -> bool:
    """Detect compound lists, PDFs, crowded open-goal trays, or operator pushback requiring `medium` thinking."""
    if has_pdf or open_goals_count >= 3:
        return True
    txt = (text or "").strip()
    if not txt:
        return False
    low = txt.lower()
    if (
        len(txt) >= 120
        or txt.count(",") >= 2
        or ";" in txt
        or "+" in txt
        or txt.count("\n") >= 2
        or " aur " in low
    ):
        return True
    return bool(_COMPLEX_PUSHBACK_RE.search(txt))


def select_hydrogen_thinking_level(
    *,
    ocr_difficulty_score: float | None = None,
    is_error_diagnosis: bool = False,
    is_multi_item_dump: bool = False,
) -> ThinkingLevel:
    """Explicitly choose thinking_level so gemini-4-hydrogen never defaults to 'high'."""
    if ocr_difficulty_score is not None and ocr_difficulty_score >= 1.5:
        return "high"
    if is_error_diagnosis or is_multi_item_dump or (
        ocr_difficulty_score is not None and ocr_difficulty_score >= 0.7
    ):
        return "medium"
    return "low"


_PURE_SOCIAL_CHATTER: frozenset[str] = frozenset(
    {
        "good morning",
        "gm",
        "good night",
        "gn",
        "ram ram",
        "jai jinendra",
        "jai shree krishna",
        "happy diwali",
        "ok",
        "okay",
        "k",
        "kk",
        "hmm",
        "acha",
        "accha",
        "thik hai",
        "theek hai",
        "thanks",
        "thank you",
        "👍",
        "🙏",
    }
)


def _parse_goal_utc(raw: Any) -> datetime | None:
    """Parse an ISO timestamp from an agent_goals row into UTC datetime."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def rank_open_goals_for_turn(
    open_goal_rows: list[dict[str, Any]],
    *,
    owner_person_id: str | None,
    telegram_chat_id: int | None,
    domain_scope: str,
    reply_to_bot_msg_id: int | None = None,
    max_goals: int = 4,
) -> list[dict[str, Any]]:
    """Rank active open goals by reply match, channel match, and operator ownership."""
    target_reply_mid = _safe_int(reply_to_bot_msg_id)
    target_chat_id = _safe_int(telegram_chat_id)
    candidates: list[tuple[int, int, float, int, dict[str, Any]]] = []

    for idx, row in enumerate(open_goal_rows):
        row_chat_id = _safe_int(row.get("telegram_chat_id"))
        row_bot_mid = _safe_int(row.get("telegram_bot_msg_id"))
        row_owner = row.get("owner_person_id")
        row_domain = row.get("domain_scope")

        tier: int | None = None
        if (
            target_reply_mid is not None
            and row_bot_mid is not None
            and row_bot_mid == target_reply_mid
            and (target_chat_id is None or row_chat_id in (None, target_chat_id))
        ):
            tier = 0
        elif (
            target_chat_id is not None
            and row_chat_id == target_chat_id
            and owner_person_id is not None
            and row_owner == owner_person_id
        ):
            tier = 1
        elif target_chat_id is not None and row_chat_id == target_chat_id:
            tier = 2
        elif (owner_person_id is None or row_owner == owner_person_id) and row_domain in (
            None,
            domain_scope,
            "shared",
        ):
            tier = 3

        if tier is None:
            continue

        ts = _parse_goal_utc(row.get("updated_at")) or _parse_goal_utc(row.get("created_at"))
        has_ts = 0 if ts is not None else 1
        neg_ts = -ts.timestamp() if ts is not None else 0.0
        candidates.append((tier, has_ts, neg_ts, idx, row))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[4] for item in candidates[:max_goals]]


def build_compact_open_goals_tray(
    open_goal_rows: list[dict[str, Any]],
    max_goals: int = 4,
    reply_to_bot_msg_id: int | None = None,
) -> list[dict[str, Any]]:
    """Build an O(1) ~150-token active-goal tray so Hydrogen handles compound replies + new tasks."""
    target_reply_mid = _safe_int(reply_to_bot_msg_id)
    tray: list[dict[str, Any]] = []
    for row in open_goal_rows[:max_goals]:
        entry: dict[str, Any] = {
            "goal_id": str(row.get("id", "")),
            "goal_type": row.get("goal_type"),
            "status": row.get("status"),
            "summary": (row.get("summary_title") or "")[:100],
            "missing_slots": list(row.get("missing_slots") or []),
            "pending_prompt": (row.get("pending_prompt") or "")[:140],
            "collected_slots": row.get("collected_slots") or {},
        }
        row_bot_mid = _safe_int(row.get("telegram_bot_msg_id"))
        if target_reply_mid is not None and row_bot_mid is not None and row_bot_mid == target_reply_mid:
            entry["replied_directly"] = True
        tray.append(entry)
    return tray


def should_bypass_group_triage_for_open_goal(
    *,
    text: str,
    incoming_message_id: int | None,
    reply_to_message_id: int | None,
    actor_person_id: str | None,
    chat_id: int,
    open_goals: list[dict[str, Any]],
    now_utc: datetime | None = None,
    fresh_window_seconds: int = 900,
) -> bool:
    """Bypass Decisions chatter filter for direct bot replies, immediate next answers, or fresh open prompts."""
    clean = re.sub(r"\s+", " ", (text or "").strip()).lower().strip("!.?,")
    if not clean:
        return False

    target_reply_mid = _safe_int(reply_to_message_id)
    target_incoming_mid = _safe_int(incoming_message_id)
    target_chat_id = _safe_int(chat_id)

    if target_reply_mid is not None:
        for g in open_goals:
            bot_mid = _safe_int(g.get("telegram_bot_msg_id"))
            g_chat = _safe_int(g.get("telegram_chat_id"))
            if (
                bot_mid is not None
                and bot_mid == target_reply_mid
                and (target_chat_id is None or g_chat in (None, target_chat_id))
            ):
                return True

    if clean in _PURE_SOCIAL_CHATTER:
        return False

    ref_now = now_utc or datetime.now(timezone.utc)
    for g in open_goals:
        if g.get("status") not in ("gathering_info", "needs_error_fix"):
            continue
        if _safe_int(g.get("telegram_chat_id")) != target_chat_id:
            continue

        bot_mid = _safe_int(g.get("telegram_bot_msg_id"))
        if target_incoming_mid is not None and bot_mid is not None:
            gap = target_incoming_mid - bot_mid
            if 0 < gap <= 2:
                return True

        if actor_person_id and g.get("owner_person_id") == actor_person_id:
            ts = _parse_goal_utc(g.get("updated_at")) or _parse_goal_utc(g.get("created_at"))
            if ts and 0 <= (ref_now - ts).total_seconds() <= fresh_window_seconds:
                return True

    return False


# ---------------------------------------------------------------------------
# Hardening #3: Atomic Compare-and-Swap & 2.5s Burst Merging
# ---------------------------------------------------------------------------
def claim_goal_for_execution(
    sb: Any,
    goal_id: str,
    allowed_prior_statuses: tuple[str, ...] = (
        "awaiting_confirmation",
        "gathering_info",
        "needs_error_fix",
        "parked_for_clerk",
    ),
) -> dict[str, Any] | None:
    """Atomically transition goal -> 'executing' so double-taps on 4G never double-post."""
    res = (
        sb.table("agent_goals")
        .update({"status": "executing"})
        .eq("id", goal_id)
        .in_("status", list(allowed_prior_statuses))
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def merge_buffered_telegram_burst(
    buffered_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine rapid-fire consecutive messages from (chat_id, user_id) into one turn."""
    texts: list[str] = []
    photo_file_ids: list[str] = []
    voice_file_ids: list[str] = []
    document_file_ids: list[str] = []
    reply_to_message_id: int | None = None
    latest_message_id: int | None = None

    for row in sorted(buffered_rows, key=lambda r: int(r.get("telegram_update_id", 0))):
        payload = row.get("raw_payload") or {}
        msg = payload.get("message") or payload.get("edited_message") or {}
        if msg.get("message_id"):
            latest_message_id = int(msg["message_id"])
        reply_mid = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_mid and reply_to_message_id is None:
            reply_to_message_id = int(reply_mid)

        txt = (msg.get("text") or msg.get("caption") or "").strip()
        if txt:
            texts.append(txt)
        if msg.get("photo"):
            photo_file_ids.append(msg["photo"][-1]["file_id"])
        voice = msg.get("voice") or msg.get("audio")
        if voice and voice.get("file_id"):
            voice_file_ids.append(voice["file_id"])
        doc = msg.get("document")
        if doc and doc.get("file_id"):
            document_file_ids.append(doc["file_id"])

    return {
        "combined_text": "\n".join(texts).strip(),
        "photo_file_ids": photo_file_ids,
        "voice_file_ids": voice_file_ids,
        "document_file_ids": document_file_ids,
        "reply_to_message_id": reply_to_message_id,
        "latest_message_id": latest_message_id,
        "update_count": len(buffered_rows),
    }


# ---------------------------------------------------------------------------
# Hardening #4: Missing-Slot Checker for New Customer Onboarding (X, Y, Z)
# ---------------------------------------------------------------------------
def compute_missing_customer_onboarding_slots(slots: dict[str, Any]) -> list[str]:
    """Require strictly Full Name + Site Area/Landmark + Rough Scope for new customers."""
    missing: list[str] = []
    for key in NEW_CUSTOMER_REQUIRED_SLOTS:
        val = slots.get(key)
        if val is None or not str(val).strip():
            missing.append(key)
    return missing


# ---------------------------------------------------------------------------
# Hardening #5: Zoho Standard Plan Idempotency Key & Error Classifier
# ---------------------------------------------------------------------------
def make_zoho_reference_number(goal_id: str, step_index: int) -> str:
    """Deterministic <=36-char Zoho reference_number so retries never double-post."""
    compact = goal_id.replace("-", "")[:16].upper()
    return f"MUNIM-{compact}-S{step_index}"


def build_zoho_standard_order_tags(
    *,
    project_code: str | None,
    zoho_tag_id: str | None,
    zoho_tag_option_id: str | None,
    custom_field_api_name: str = "cf_project_code",
) -> dict[str, Any]:
    """Build Zoho Books Standard Plan (~₹749/mo) Reporting Tag + Custom Field payload."""
    extra: dict[str, Any] = {}
    if zoho_tag_id and zoho_tag_option_id:
        extra["tags"] = [{"tag_id": zoho_tag_id, "tag_option_id": zoho_tag_option_id}]
    if project_code and custom_field_api_name:
        extra["custom_fields"] = [
            {"api_name": custom_field_api_name, "value": project_code}
        ]
    return extra


def classify_external_error(
    status_code: int | None, is_timeout: bool = False
) -> StepErrorKind:
    """Split transient infra errors (silent Cloud Tasks backoff) vs 400 data errors (LLM repair)."""
    if is_timeout or status_code is None:
        return "transient_5xx"
    if status_code == 429 or status_code >= 500:
        return "transient_5xx"
    if 400 <= status_code < 500:
        return "validation_400"
    return "none"


# ---------------------------------------------------------------------------
# Hardening #6: Mandatory Voice Transcript Echo + Indian Rupee Formatter
# ---------------------------------------------------------------------------
def format_voice_receipt_header(heard_transcript: str | None) -> str:
    """Always echo the recognized Hinglish voice line at the top of voice replies."""
    clean = (heard_transcript or "").strip()
    if not clean:
        return ""
    return f'🎙️ Heard: _"{clean}"_\n'


def format_inr(amount: float | Decimal | int) -> str:
    """Format monetary values using Indian en-IN grouping (e.g., ₹23,00,000 or ₹1,15,000.50)."""
    quant = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quant < 0 else ""
    abs_val = abs(quant)
    int_part = int(abs_val)
    frac_part = abs_val - Decimal(int_part)

    s = str(int_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        grouped = ",".join(groups) + "," + last3
    else:
        grouped = s

    if frac_part > 0:
        frac_str = f"{frac_part:.2f}"[1:]
        return f"{sign}₹{grouped}{frac_str}"
    return f"{sign}₹{grouped}"


# ---------------------------------------------------------------------------
# Reversible 1-Tap [↩️ Undo] Snapshot Builder for Production Stage Moves
# ---------------------------------------------------------------------------
def build_stage_undo_snapshot(
    *,
    track: Literal["pipeline", "material", "payment", "item"],
    row_before: dict[str, Any],
) -> dict[str, Any]:
    """Capture exact pre-mutation columns so 1-tap [Undo] restores cleanly."""
    if track == "pipeline":
        return {
            "pipeline_stage": row_before.get("pipeline_stage"),
            "pipeline_stage_at": row_before.get("pipeline_stage_at"),
            "current_owner_ids": list(row_before.get("current_owner_ids") or []),
            "estimator_ids": list(row_before.get("estimator_ids") or []),
            "sizing_mistri_id": row_before.get("sizing_mistri_id"),
            "boq_scope_changed": bool(row_before.get("boq_scope_changed", False)),
        }
    if track == "material":
        return {
            "material_stage": row_before.get("material_stage"),
            "material_stage_at": row_before.get("material_stage_at"),
            "material_owner_id": row_before.get("material_owner_id"),
        }
    if track == "payment":
        return {
            "payment_stage": row_before.get("payment_stage"),
            "payment_stage_at": row_before.get("payment_stage_at"),
        }
    return {
        "stage": row_before.get("stage"),
        "stage_entered_at": row_before.get("stage_entered_at"),
        "production_mistri_id": row_before.get("production_mistri_id"),
        "assembly_mistri_id": row_before.get("assembly_mistri_id"),
        "qc_passed_by_id": row_before.get("qc_passed_by_id"),
        "qc_passed_at": row_before.get("qc_passed_at"),
    }


def merge_unique_str_list(
    existing: list[str] | None, additions: list[str] | None
) -> list[str]:
    """Append non-empty strings case-insensitively without wiping existing aliases."""
    result = [str(x).strip() for x in (existing or []) if str(x).strip()]
    seen_lower = {x.lower() for x in result}
    for item in additions or []:
        clean = str(item).strip()
        if clean and clean.lower() not in seen_lower:
            result.append(clean)
            seen_lower.add(clean.lower())
    return result


def is_high_sensitivity_master_mutation(
    operation_kind: str,
    slots: dict[str, Any],
    current_row: dict[str, Any] | None = None,
) -> bool:
    """Classify whether a Master Spine / Order mutation requires Before->After [Confirm]
    vs Auto-Commit + 1-Tap [Undo]:
      - Confirm-Gated: new records, soft-archiving, ₹ quoted_total changes, renames,
        GSTIN changes, and worker bank payout_aliases changes.
      - Auto-Commit + Undo: phone, area/landmark, specialty, notes, voice aliases,
        promised_delivery_date, rough_scope, boq_scope_changed/note, role_tags.
    """
    if operation_kind in ("new_lead", "add_customer", "add_person_or_vendor", "archive_record"):
        return True

    row = current_row or {}

    if operation_kind == "update_order_details":
        if slots.get("quoted_total") is not None:
            cur_q = round(float(row.get("quoted_total") or 0.0), 2)
            new_q = round(float(slots["quoted_total"]), 2)
            if new_q != cur_q:
                return True
        return False

    # Check entity rename against existing DB name
    cur_name = str(row.get("full_name") or row.get("canonical_name") or "").strip()
    proposed_rename = str(slots.get("new_name") or "").strip()
    if proposed_rename and proposed_rename.lower() != cur_name.lower():
        return True

    if operation_kind == "update_vendor":
        if slots.get("gstin") and str(slots["gstin"]).strip().upper() != str(row.get("gstin") or "").strip().upper():
            return True
        if slots.get("person_or_vendor_kind") and slots["person_or_vendor_kind"] in ("supplier", "contractor"):
            if slots["person_or_vendor_kind"] != row.get("vendor_kind"):
                return True
        return False

    if operation_kind == "update_person":
        if slots.get("payout_aliases"):
            existing_pa = {str(x).strip().lower() for x in (row.get("payout_aliases") or [])}
            incoming_pa = {str(x).strip().lower() for x in (slots.get("payout_aliases") or []) if str(x).strip()}
            if incoming_pa - existing_pa:
                return True
        if slots.get("person_or_vendor_kind") and slots["person_or_vendor_kind"] in (
            "operator",
            "salaried_karigar",
            "contractor_mistri",
        ):
            if slots["person_or_vendor_kind"] != row.get("person_type"):
                return True
        return False

    return False


# ---------------------------------------------------------------------------
# Domain 2 HR: Worker Photo Attendance Worked Minutes & >6:00 PM IST Overtime
# ---------------------------------------------------------------------------
def compute_ist_attendance_minutes(
    check_in_at: datetime,
    check_out_at: datetime,
    shift_end_ist: dt_time = dt_time(18, 0),
) -> tuple[int, int]:
    """Return (worked_minutes, overtime_minutes_past_6pm_ist) from check-in/out timestamps."""
    in_ist = check_in_at.astimezone(IST)
    out_ist = check_out_at.astimezone(IST)
    if out_ist <= in_ist:
        return 0, 0

    worked_minutes = int((out_ist - in_ist).total_seconds() // 60)
    cutoff_ist = datetime.combine(in_ist.date(), shift_end_ist, tzinfo=IST)
    ot_start = max(in_ist, cutoff_ist)
    overtime_minutes = (
        int((out_ist - ot_start).total_seconds() // 60) if out_ist > ot_start else 0
    )
    return worked_minutes, overtime_minutes


# ---------------------------------------------------------------------------
# Munim Expense Categorization & Zoho Chart-of-Accounts Routing
# ---------------------------------------------------------------------------
EXPENSE_CATEGORIES: dict[str, dict[str, Any]] = {
    "salary_wages": {
        "label": "Salaries & Wages",
        "zoho_names": ["salaries and employee wages", "salaries", "wages"],
        "pattern": r"\b(salary|salaries|tankhwah|tankha|pagar|wages|wage|dihadi|fnf|bonus|karigar\s+advance|salary\s+advance|staff\s+advance)\b",
    },
    "contractor_labor": {
        "label": "Contractor / Site Labor",
        "zoho_names": ["subcontractor", "labor", "job costing", "consultant expense"],
        "pattern": r"\b(majdoori|mazdoori|labour|labor|theka|contractor|fitting\s+charge|polish\s+labour|unloading\s+labour|loading\s+labour|cushioning\s+charge)\b",
    },
    "transport_freight": {
        "label": "Transport & Freight",
        "zoho_names": ["transportation expense", "automobile expense", "travel expense"],
        "pattern": r"\b(tempo|auto|truck|pickup|transport|transportation|bhada|bhaada|freight|cartage|petrol|diesel|fuel|toll|riksa|rickshaw)\b",
    },
    "raw_materials": {
        "label": "Raw Materials & Hardware (COGS)",
        "zoho_names": ["materials", "cost of goods sold", "raw materials and consumables", "merchandise"],
        "pattern": r"\b(ply|plywood|hdhmr|mdf|board|laminate|sunmica|mica|veneer|acrylic|hardware|hinge|hinges|channel|screw|screws|fevicol|adhesive|glue|edgeband|pvc\s+tape|foam|fabric|glass|mirror|handle|handles|lock|locks)\b",
    },
    "food_tea_mess": {
        "label": "Tea, Mess & Staff Welfare",
        "zoho_names": ["meals and entertainment"],
        "pattern": r"\b(chai|tea|coffee|nasta|nashta|snacks|lunch|dinner|khana|mess|ration|water\s+camper|pani|milk|doodh)\b",
    },
    "rent": {
        "label": "Shop / Godown Rent",
        "zoho_names": ["rent expense"],
        "pattern": r"\b(rent|godown\s+rent|shop\s+rent|dukan\s+kiraya|godam\s+kiraya)\b",
    },
    "utilities_office": {
        "label": "Electricity, Phone & Office",
        "zoho_names": ["telephone expense", "it and internet expenses", "office supplies", "printing and stationery"],
        "pattern": r"\b(electricity|bijli|light\s+bill|power\s+bill|internet|wifi|broadband|recharge|mobile\s+bill|telephone|stationery|printing|paper|pen|office)\b",
    },
    "repairs_maintenance": {
        "label": "Machine & Shop Maintenance",
        "zoho_names": ["repairs and maintenance", "janitorial expense"],
        "pattern": r"\b(repair|repairs|maintenance|blade|dhaar|sharpening|cutter|compressor|machine|tool\s+repair|welding|cleaning|safai|plumber|electrician)\b",
    },
    "other_expense": {
        "label": "Other Workshop Expense",
        "zoho_names": ["other expenses", "uncategorized"],
        "pattern": "",
    },
}

_EXPENSE_CATEGORY_ALIASES: dict[str, str] = {
    "salary": "salary_wages",
    "salaries": "salary_wages",
    "salaries_and_wages": "salary_wages",
    "salaries_and_employee_wages": "salary_wages",
    "salary_advance": "salary_wages",
    "wages": "salary_wages",
    "payroll": "salary_wages",
    "labor": "contractor_labor",
    "labour": "contractor_labor",
    "subcontractor": "contractor_labor",
    "contractor": "contractor_labor",
    "transport": "transport_freight",
    "transportation": "transport_freight",
    "transportation_expense": "transport_freight",
    "freight": "transport_freight",
    "travel": "transport_freight",
    "fuel": "transport_freight",
    "material": "raw_materials",
    "materials": "raw_materials",
    "raw_material": "raw_materials",
    "raw_materials_and_consumables": "raw_materials",
    "raw_materials_hardware_cogs": "raw_materials",
    "cogs": "raw_materials",
    "cost_of_goods_sold": "raw_materials",
    "hardware": "raw_materials",
    "food": "food_tea_mess",
    "tea": "food_tea_mess",
    "mess": "food_tea_mess",
    "meals": "food_tea_mess",
    "meals_and_entertainment": "food_tea_mess",
    "shop_rent": "rent",
    "godown_rent": "rent",
    "rent_expense": "rent",
    "utilities": "utilities_office",
    "electricity": "utilities_office",
    "internet": "utilities_office",
    "telephone": "utilities_office",
    "office": "utilities_office",
    "stationery": "utilities_office",
    "repairs": "repairs_maintenance",
    "repair": "repairs_maintenance",
    "maintenance": "repairs_maintenance",
    "other": "other_expense",
    "other_expenses": "other_expense",
}

_INFER_SCAN_ORDER: tuple[str, ...] = (
    "salary_wages",
    "rent",
    "transport_freight",
    "food_tea_mess",
    "repairs_maintenance",
    "utilities_office",
    "contractor_labor",
    "raw_materials",
)


def normalize_or_infer_expense_category(
    raw_category: str | None, *texts: str | None
) -> str:
    """Resolve canonical expense category key from LLM hint and/or Hinglish text cues."""
    if raw_category and str(raw_category).strip():
        key = re.sub(r"[\s/&,-]+", "_", str(raw_category).strip().lower()).strip("_")
        if key in EXPENSE_CATEGORIES and key != "other_expense":
            return key
        mapped = _EXPENSE_CATEGORY_ALIASES.get(key)
        if mapped and mapped != "other_expense":
            return mapped

    combined = " ".join((t or "").strip().lower() for t in texts if t and str(t).strip())
    if combined:
        for code in _INFER_SCAN_ORDER:
            pat = EXPENSE_CATEGORIES[code]["pattern"]
            if pat and re.search(pat, combined):
                return code
    return "other_expense"


def get_expense_category_label(code: str | None) -> str:
    """Return human-readable workshop expense category label."""
    if code and code in EXPENSE_CATEGORIES:
        return str(EXPENSE_CATEGORIES[code]["label"])
    return str(EXPENSE_CATEGORIES["other_expense"]["label"])
