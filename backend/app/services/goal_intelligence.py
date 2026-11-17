"""Goal Intelligence Upgrades (#2, #3, #4, #6) for AgentCore.

1. Feature #2 (`verify_committed_goal_steps`):
   Post-write self-verification read-back checks against Supabase/Zoho step results
   before closing an `agent_goals` row as `completed`.
2. Feature #3 (`build_side_quest_parked_goal_nudge`):
   Quiet 1-line parked-goal reminder appended after an operator completes a side-quest
   or Q&A turn (strictly scoped to the same `owner_person_id`).
3. Feature #4 (`enqueue_goal_retry_task` & `execute_scheduled_goal_retry`):
   Automatic Cloud Tasks v2 REST background retry with exponential backoff on transient
   Zoho `429 / 5xx` errors, capped at `MAX_AUTO_ZOHO_RETRIES = 3`.
4. Feature #6 (`maybe_distill_procedural_memory`):
   Fast deterministic pre-gate + `gemini-decision-1` worthiness check + low-thinking
   `gemini-4-hydrogen` distillation into `workshop_memory` and master aliases.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Literal
import uuid

from google import genai
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services import decisions_triage, telegram_api
from app.services.goal_primitives import merge_unique_str_list

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature #4 & Rolling Conversational Memory Constants
# ---------------------------------------------------------------------------
CONVERSATION_WINDOW_MINUTES = 30
MAX_CONVERSATION_TURNS = 10

MAX_AUTO_ZOHO_RETRIES = 3
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (15.0, 60.0, 240.0)

_TRANSIENT_MARKERS: tuple[str, ...] = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "zoho request failed",
    "temporarily",
    "connection",
)

# ---------------------------------------------------------------------------
# Feature #6 Pre-Gate Key Sets
# ---------------------------------------------------------------------------
_ONE_OFF_NUMERIC_OR_DATE_KEYS: set[str] = {
    "amount_inr",
    "txn_date",
    "invoice_no",
    "promised_delivery_date",
    "quoted_total",
    "dimensions",
    "to_stage",
    "boq_scope_changed",
    "area_landmark",
    "rough_scope",
    "notes",
    "specs_notes",
    "initial_items_json",
    "material_stage",
    "item_name",
}

_LEARNABLE_MAPPING_KEYS: set[str] = {
    "payment_account_code",
    "payment_account_id",
    "to_payment_account_code",
    "to_payment_account_id",
    "vendor_id",
    "customer_id",
    "person_id",
    "party_name",
    "aliases",
    "payout_aliases",
    "production_mistri_id",
    "assembly_mistri_id",
    "sizing_mistri_id",
    "estimator_ids",
    "new_name",
    "specialty",
    "role_tags",
    "gstin",
    "phone",
}

_REUSABLE_HINT_WORDS: tuple[str, ...] = (
    "hamesha",
    "always",
    "default",
    "usually",
    "alias",
    "proxy",
    "account mein",
    "remember",
    "yaad rakh",
    "means",
    "matlab",
)


# ---------------------------------------------------------------------------
# Schema for Feature #6 LLM Extraction
# ---------------------------------------------------------------------------
class DistilledMemoryArtifact(BaseModel):
    should_save: bool = Field(
        description="True ONLY if this teaches a reusable workshop convention, default account/vendor, or name/payout alias."
    )
    rule_text: str = Field(default="", max_length=160)
    category: Literal["entity", "account", "workflow", "style"] = "entity"
    alias_target_table: Literal["none", "customers", "vendors", "people"] = "none"
    alias_target_id: str | None = None
    alias_value: str | None = None
    is_payout_alias: bool = False


def _inline_schema(
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
            return {
                k: _resolve(v)
                for k, v in node.items()
                if k not in ("title", "default")
            }
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


def _fetch_one(sb: Any, table: str, record_id: str | None) -> dict[str, Any] | None:
    if not record_id:
        return None
    rows = sb.table(table).select("*").eq("id", record_id).limit(1).execute().data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Feature #2: Post-Write Self-Verification (`verify_committed_goal_steps`)
# ---------------------------------------------------------------------------
def _verify_single_step(
    sb: Any, *, step: dict[str, Any], slots: dict[str, Any]
) -> str | None:
    """Return None when committed DB/Zoho state matches intent, else a mismatch string."""
    op = str(step.get("operation") or "")
    res = step.get("result") or {}
    if op != str(slots.get("operation_kind") or ""):
        merged = {
            **slots,
            **{k: v for k, v in (step.get("payload") or {}).items() if v is not None},
        }
    else:
        merged = {**(step.get("payload") or {}), **slots}

    if op == "pipeline_stage_move":
        row = _fetch_one(sb, "projects", merged.get("project_id"))
        if not row:
            return "Updated project could not be read back from database."
        if row.get("pipeline_stage") != merged.get("to_stage"):
            return (
                f"Expected pipeline_stage='{merged.get('to_stage')}' "
                f"but found '{row.get('pipeline_stage')}'."
            )
        return None

    if op == "material_stage_move":
        row = _fetch_one(sb, "projects", merged.get("project_id"))
        if not row:
            return "Updated project could not be read back for material check."
        if row.get("material_stage") != merged.get("to_stage"):
            return (
                f"Expected material_stage='{merged.get('to_stage')}' "
                f"but found '{row.get('material_stage')}'."
            )
        return None

    if op == "item_upsert_or_move":
        item_id = res.get("item_id") or merged.get("item_id")
        row = _fetch_one(sb, "project_items", item_id)
        if not row:
            return f"Furniture item '{item_id or merged.get('item_name')}' missing after save."
        expected_stage = merged.get("to_stage") or "in_shop"
        if row.get("stage") != expected_stage:
            return f"Expected item stage='{expected_stage}' but found '{row.get('stage')}'."
        if merged.get("production_mistri_id") and row.get("production_mistri_id") != merged.get("production_mistri_id"):
            return "Item production_mistri_id did not match requested Mistri."
        return None

    if op == "item_material_upsert":
        mat_ids = [str(mid) for mid in (res.get("material_ids") or []) if mid]
        if not mat_ids:
            return "No raw material IDs returned after save."
        rows = (
            sb.table("item_materials")
            .select("id,status")
            .in_("id", mat_ids)
            .execute()
            .data
            or []
        )
        if len(rows) < len(mat_ids):
            return f"Expected {len(mat_ids)} raw material row(s), found {len(rows)} after save."
        if merged.get("material_status"):
            from app.services.step_executors import normalize_material_status

            expected_st = normalize_material_status(merged["material_status"])
            for r in rows:
                if r.get("status") != expected_st:
                    return f"Expected material status='{expected_st}' but found '{r.get('status')}'."
        return None

    if op == "new_lead":
        prj_id = res.get("project_id") or merged.get("project_id")
        cust_id = res.get("customer_id") or merged.get("customer_id")
        prj_row = _fetch_one(sb, "projects", prj_id)
        cust_row = _fetch_one(sb, "customers", cust_id)
        if not prj_row or not cust_row:
            return "Lead project or customer row missing after creation."
        if prj_row.get("zoho_tag_option_id") and prj_row.get("pipeline_stage") in (
            None,
            "lead",
            "design",
            "estimating",
            "quoted_awaiting_advance",
        ):
            return "Pre-sales Lead unexpectedly received a Zoho Reporting Tag."
        return None

    if op in ("add_customer", "update_customer"):
        cust_id = res.get("customer_id") or merged.get("customer_id")
        row = _fetch_one(sb, "customers", cust_id)
        if not row:
            return f"Customer '{cust_id}' missing after {op}."
        if op == "update_customer" and merged.get("new_name"):
            expected_name = str(merged["new_name"]).strip().lower()
            actual_name = str(row.get("full_name") or "").strip().lower()
            if actual_name != expected_name:
                return f"Expected customer rename to '{merged['new_name']}', found '{row.get('full_name')}'."
        return None

    if op == "update_vendor":
        row = _fetch_one(sb, "vendors", merged.get("vendor_id"))
        if not row:
            return f"Vendor '{merged.get('vendor_id')}' missing after update."
        if merged.get("new_name"):
            expected_name = str(merged["new_name"]).strip().lower()
            actual_name = str(row.get("canonical_name") or "").strip().lower()
            if actual_name != expected_name:
                return f"Expected vendor rename to '{merged['new_name']}', found '{row.get('canonical_name')}'."
        return None

    if op == "update_person":
        row = _fetch_one(sb, "people", merged.get("person_id"))
        if not row:
            return f"Person '{merged.get('person_id')}' missing after update."
        if merged.get("new_name"):
            expected_name = str(merged["new_name"]).strip().lower()
            actual_name = str(row.get("full_name") or "").strip().lower()
            if actual_name != expected_name:
                return f"Expected person rename to '{merged['new_name']}', found '{row.get('full_name')}'."
        return None

    if op == "update_order_details":
        row = _fetch_one(sb, "projects", merged.get("project_id"))
        if not row:
            return f"Order '{merged.get('project_id')}' missing after metadata update."
        if merged.get("quoted_total") is not None:
            actual_q = float(row.get("quoted_total") or 0)
            expected_q = float(merged["quoted_total"])
            if abs(actual_q - expected_q) > 0.05:
                return f"Expected quoted_total={expected_q:.2f}, found {actual_q:.2f}."
        return None

    if op == "archive_record":
        kind = str(merged.get("archive_entity_kind") or "").lower()
        target_map = {
            "customer": ("customers", "customer_id"),
            "vendor": ("vendors", "vendor_id"),
            "person": ("people", "person_id"),
            "order": ("projects", "project_id"),
        }
        spec = target_map.get(kind)
        if not spec:
            return f"Unknown archive_entity_kind '{kind}'."
        row = _fetch_one(sb, spec[0], merged.get(spec[1]))
        if not row:
            return f"Archived {kind} row not found in '{spec[0]}'."
        if kind == "order":
            if row.get("pipeline_stage") != "lost" and row.get("status") != "lost":
                return f"Expected order to be marked 'lost', found pipeline='{row.get('pipeline_stage')}'."
        elif row.get("is_active") is not False:
            return f"Expected {kind} is_active=False after archive, found is_active={row.get('is_active')}."
        return None

    if op == "add_person_or_vendor":
        if res.get("vendor_id"):
            if not _fetch_one(sb, "vendors", res.get("vendor_id")):
                return f"Inserted vendor '{res.get('vendor_id')}' missing after save."
        elif res.get("person_id"):
            if not _fetch_one(sb, "people", res.get("person_id")):
                return f"Inserted person '{res.get('person_id')}' missing after save."
        else:
            return "Neither vendor_id nor person_id was returned by onboarding step."
        return None

    if op in ("customer_receipt", "customer_payment", "vendor_payment", "expense", "float_transfer"):
        ref_no = str(res.get("reference_number") or "")
        if not ref_no.startswith("MUNIM-"):
            return f"Financial entry missing deterministic MUNIM-* reference_number (got '{ref_no}')."
        if op in ("customer_receipt", "customer_payment") and merged.get("project_id") and float(merged.get("amount_inr") or 0) > 0:
            prj = _fetch_one(sb, "projects", merged.get("project_id"))
            if not prj:
                return f"Linked project '{merged.get('project_id')}' missing during receipt verification."
            if prj.get("payment_stage") == "advance_pending":
                return "Customer receipt did not advance Order Payment Track past 'advance_pending'."
        return None

    if op in ("supplier_draft_bill", "customer_draft_invoice"):
        draft_status = str(res.get("draft_status") or "draft").strip().lower()
        if draft_status != "draft":
            return f"Safety invariant violation: supplier bill status must remain 'draft', got '{draft_status}'."
        return None

    return None


def verify_committed_goal_steps(
    sb: Any,
    *,
    steps: list[dict[str, Any]],
    slots: dict[str, Any],
) -> dict[str, Any]:
    """Read back DB/Zoho step effects to confirm final state matches user intent."""
    checks_run = 0
    for idx, step in enumerate(steps):
        if step.get("status") != "succeeded":
            continue
        checks_run += 1
        mismatch = _verify_single_step(sb, step=step, slots=slots)
        if mismatch:
            return {
                "verified": False,
                "checks_run": checks_run,
                "failed_step_index": idx,
                "mismatch": mismatch,
            }
    return {"verified": True, "checks_run": checks_run}


# ---------------------------------------------------------------------------
# Feature #3: 1-Line Parked-Goal Nudge After Side-Quests
# ---------------------------------------------------------------------------
def build_side_quest_parked_goal_nudge(
    sb: Any,
    *,
    owner_person_id: str | None,
    touched_goal_ids: set[str],
) -> dict[str, Any] | None:
    """Return a single-line reminder for the same operator's older parked goal, if any."""
    if not owner_person_id:
        return None

    rows = (
        sb.table("agent_goals")
        .select("id,summary_title,pending_prompt,status,updated_at")
        .eq("owner_person_id", owner_person_id)
        .in_("status", ["gathering_info", "needs_error_fix"])
        .order("updated_at", desc=True)
        .limit(5)
        .execute()
        .data
        or []
    )
    remaining = [r for r in rows if str(r.get("id")) not in touched_goal_ids]
    if not remaining:
        return None

    top = remaining[0]
    title = str(top.get("summary_title") or "Pending task").strip()
    prompt = str(top.get("pending_prompt") or "Needs one quick detail to finish.").strip()
    compact_prompt = " ".join(prompt.splitlines()).strip()
    if len(compact_prompt) > 120:
        compact_prompt = compact_prompt[:117] + "..."

    return {
        "goal_id": str(top["id"]),
        "summary_title": title,
        "pending_prompt": prompt,
        "nudge_line": f"📌 *Still open ({title}):* {compact_prompt}",
    }


# ---------------------------------------------------------------------------
# Feature #4: Transient Zoho Retry Scheduling & Worker Execution
# ---------------------------------------------------------------------------
def is_transient_zoho_error_message(msg: str) -> bool:
    """Classify timeout / 429 / 5xx error text as safely retryable."""
    low = (msg or "").lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def compute_retry_backoff_seconds(attempt: int) -> float:
    """Map 1-based retry attempt to exponential delay (15s, 60s, 240s)."""
    idx = min(max(attempt - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
    return RETRY_BACKOFF_SECONDS[idx]


def enqueue_goal_retry_task(
    *,
    goal_id: str,
    actor_person_id: str | None,
    actor_label: str,
    attempt: int,
) -> dict[str, Any]:
    """Schedule a delayed Cloud Tasks v2 REST callback to `/tasks/goal-retry`."""
    settings = get_settings()
    delay = compute_retry_backoff_seconds(attempt)
    if not settings.cloud_tasks_enabled:
        return {
            "queued": False,
            "mode": "local_manual_or_worker",
            "delay_seconds": delay,
        }

    try:
        import base64
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
        import httpx

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(GoogleAuthRequest())

        api_url = (
            f"https://cloudtasks.googleapis.com/v2/projects/{settings.gcp_project_id}"
            f"/locations/{settings.gcp_region}/queues/{settings.cloud_tasks_queue}/tasks"
        )
        target_url = f"{settings.app_base_url.rstrip('/')}/tasks/goal-retry"
        payload = {
            "goal_id": goal_id,
            "actor_person_id": actor_person_id,
            "actor_label": actor_label,
            "attempt": attempt,
        }
        body_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        sched_dt = datetime.now(timezone.utc) + timedelta(seconds=delay)
        task_obj = {
            "httpRequest": {
                "httpMethod": "POST",
                "url": target_url,
                "headers": {
                    "Content-Type": "application/json",
                    "X-Cron-Secret": settings.cron_auth_secret,
                },
                "body": body_b64,
            },
            "scheduleTime": sched_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        resp = httpx.post(
            api_url,
            headers={"Authorization": f"Bearer {creds.token}"},
            json={"task": task_obj},
            timeout=10.0,
        )
        resp.raise_for_status()
        return {"queued": True, "mode": "cloud_tasks_rest", "delay_seconds": delay}
    except Exception as exc:
        logger.warning("Failed to enqueue Cloud Tasks goal retry for %s: %s", goal_id, exc)
        return {"queued": False, "mode": "cloud_tasks_error", "error": str(exc), "delay_seconds": delay}


async def execute_scheduled_goal_retry(
    sb: Any,
    *,
    goal_id: str,
    actor_person_id: str | None = None,
    actor_label: str = "Auto-Retry",
) -> dict[str, Any]:
    """Run one background Cloud Tasks retry for an `awaiting_confirmation` Zoho goal."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found"}
    row = rows[0]
    if row.get("status") != "awaiting_confirmation":
        return {"ok": True, "skipped": row.get("status")}

    from app.services import agent_core

    out = await agent_core.confirm_and_execute_goal(
        sb,
        goal_id=goal_id,
        actor_person_id=actor_person_id or row.get("owner_person_id"),
        actor_label=actor_label,
        source_channel="system",
    )

    if (
        out.get("status") == "completed"
        and row.get("telegram_chat_id")
        and row.get("telegram_bot_msg_id")
        and get_settings().telegram_bot_token
    ):
        try:
            kb = (
                {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"ag_undo:{goal_id}"}]]}
                if out.get("undo_available")
                else {"inline_keyboard": []}
            )
            await telegram_api.edit_message_text(
                chat_id=row["telegram_chat_id"],
                message_id=int(row["telegram_bot_msg_id"]),
                text=f"✅ *Saved (Auto-Retry)*\n{out.get('receipt') or ''}",
                reply_markup=kb,
            )
        except Exception as exc:
            logger.warning("Telegram retry status edit failed for %s: %s", goal_id, exc)

    return out


# ---------------------------------------------------------------------------
# Feature #6: Automatic Procedural Memory & Master Alias Distillation
# ---------------------------------------------------------------------------
def _should_pass_fast_memory_pregate(
    prior_slots: dict[str, Any],
    updated_slots: dict[str, Any],
    user_correction_text: str | None,
) -> tuple[bool, set[str]]:
    changed_keys = {
        k
        for k, v in updated_slots.items()
        if v is not None and v != [] and v != "" and prior_slots.get(k) != v
    }
    hint_text = (user_correction_text or "").lower()
    has_reusable_hint = any(w in hint_text for w in _REUSABLE_HINT_WORDS)

    if not changed_keys and not has_reusable_hint:
        return False, changed_keys

    if changed_keys and changed_keys.issubset(_ONE_OFF_NUMERIC_OR_DATE_KEYS) and not has_reusable_hint:
        return False, changed_keys

    # Also skip pure empty->filled non-mapping slots (e.g., first-time lead city) unless hinted
    has_learnable_key = bool(changed_keys & _LEARNABLE_MAPPING_KEYS)
    overrides_existing_value = any(
        prior_slots.get(k) not in (None, "", []) and k not in _ONE_OFF_NUMERIC_OR_DATE_KEYS
        for k in changed_keys
    )
    if not (has_reusable_hint or has_learnable_key or overrides_existing_value):
        return False, changed_keys

    return True, changed_keys


def _append_alias_to_master_table(
    sb: Any,
    *,
    table: Literal["customers", "vendors", "people"],
    record_id: str,
    alias_value: str,
    is_payout_alias: bool,
) -> bool:
    """Append an alias (or worker bank payout alias) idempotently without overwriting existing ones."""
    clean_alias = alias_value.strip()
    if len(clean_alias) < 2:
        return False
    row = _fetch_one(sb, table, record_id)
    if not row:
        return False

    canonical_name = str(row.get("full_name") or row.get("canonical_name") or "").strip()
    if clean_alias.lower() == canonical_name.lower():
        return False

    col = "payout_aliases" if (table == "people" and is_payout_alias) else "aliases"
    merged_list = merge_unique_str_list(row.get(col) or [], [clean_alias])
    if merged_list == (row.get(col) or []):
        return False

    sb.table(table).update({col: merged_list}).eq("id", record_id).execute()
    return True


def maybe_distill_procedural_memory(
    sb: Any,
    *,
    prior_summary: str,
    prior_slots: dict[str, Any],
    updated_slots: dict[str, Any],
    user_correction_text: str | None = None,
) -> dict[str, Any]:
    """Turn reusable user corrections/edits into deduplicated `workshop_memory` rules + aliases."""
    try:
        should_run, changed_keys = _should_pass_fast_memory_pregate(
            prior_slots, updated_slots, user_correction_text
        )
        if not should_run:
            reason = "no_meaningful_diff" if not changed_keys else "one_off_transactional_edit"
            return {"saved": False, "reason": reason}

        prior_str = (
            f"{prior_summary[:140]} | prior="
            f"{json.dumps(prior_slots, ensure_ascii=False, default=str)[:240]}"
        )
        changed_slice = {k: updated_slots.get(k) for k in sorted(changed_keys)}
        corr_str = (
            f"note={(user_correction_text or '')[:180]} | changed="
            f"{json.dumps(changed_slice, ensure_ascii=False, default=str)[:240]}"
        )

        if not decisions_triage.should_distill_memory_rule(prior_str, corr_str):
            return {"saved": False, "reason": "decisions_gate_rejected"}

        settings = get_settings()
        if not settings.gemini_api_key:
            return {"saved": False, "reason": "gemini_key_missing"}

        client = _get_interactions_client()
        interaction = client.interactions.create(
            model=settings.gemini_live_model,
            system_instruction=(
                "Extract ONLY genuinely reusable workshop rules or name/payout aliases from an operator correction "
                "(e.g. 'Ruksar Bano is Lala Ji bank payout alias' or 'Laminate bills go via Ansh'). "
                "Reject one-off dates, invoice numbers, or rupee amounts by setting should_save=false."
            ),
            input=(
                f"Prior summary: {prior_summary}\n"
                f"Prior slots: {json.dumps(prior_slots, ensure_ascii=False, default=str)}\n"
                f"Updated slots: {json.dumps(updated_slots, ensure_ascii=False, default=str)}\n"
                f"User note: {user_correction_text or 'None'}"
            ),
            generation_config={"thinking_level": "low"},
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _inline_schema(
                    DistilledMemoryArtifact,
                    force_required=("should_save", "rule_text", "category"),
                ),
            },
        )
        artifact = DistilledMemoryArtifact.model_validate_json(
            getattr(interaction, "output_text", None) or "{}"
        )
        clean_rule = artifact.rule_text.strip()[:160]
        if not artifact.should_save or not clean_rule:
            return {"saved": False, "reason": "hydrogen_declined"}

        existing_rules = (
            sb.table("workshop_memory")
            .select("rule_text")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        existing_lower = {str(r.get("rule_text") or "").strip().lower() for r in existing_rules}
        if clean_rule.lower() not in existing_lower:
            sb.table("workshop_memory").insert(
                {
                    "rule_text": clean_rule,
                    "category": artifact.category,
                    "source": "auto_correction",
                    "is_active": True,
                }
            ).execute()

        alias_updated = False
        if (
            artifact.alias_target_table in ("customers", "vendors", "people")
            and artifact.alias_target_id
            and artifact.alias_value
        ):
            alias_updated = _append_alias_to_master_table(
                sb,
                table=artifact.alias_target_table,
                record_id=artifact.alias_target_id,
                alias_value=artifact.alias_value,
                is_payout_alias=artifact.is_payout_alias,
            )

        return {
            "saved": True,
            "rule_text": clean_rule,
            "category": artifact.category,
            "alias_updated": alias_updated,
        }
    except Exception as exc:
        logger.warning("Procedural memory distillation skipped safely: %s", exc)
        return {"saved": False, "reason": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Rolling 10-Turn / 30-Minute Conversational Continuity Helpers
# ---------------------------------------------------------------------------
def _parse_ts_utc(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_rolling_conversation_context(
    sb: Any,
    *,
    owner_person_id: str | None,
    source_channel: str,
    telegram_chat_id: int | None = None,
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """Load up to 10 recent turns within a 30-minute rolling inactivity window."""
    if source_channel == "web_command_bar" and not owner_person_id:
        return []
    if source_channel == "telegram" and not telegram_chat_id and not owner_person_id:
        return []

    q = (
        sb.table("agent_goals")
        .select(
            "id,goal_type,status,summary_title,pending_prompt,collected_slots,"
            "turn_digest,steps,project_id,customer_id,created_at,updated_at,heard_transcript"
        )
        .order("updated_at", desc=True)
        .limit(20)
    )
    if source_channel == "telegram":
        if telegram_chat_id:
            q = q.eq("telegram_chat_id", telegram_chat_id)
        if owner_person_id:
            q = q.eq("owner_person_id", owner_person_id)
    else:
        q = q.eq("owner_person_id", owner_person_id).eq("source_channel", "web_command_bar")

    rows = q.execute().data or []
    if not rows:
        return []

    ref_now = now_utc or datetime.now(timezone.utc)
    cutoff_delta = timedelta(minutes=CONVERSATION_WINDOW_MINUTES)

    selected: list[dict[str, Any]] = []
    prev_ts: datetime | None = None
    for idx, r in enumerate(rows):
        ts = _parse_ts_utc(r.get("updated_at") or r.get("created_at"))
        if idx == 0:
            if ts is not None and (ref_now - ts) > cutoff_delta:
                return []
        else:
            if prev_ts is not None and ts is not None and (prev_ts - ts) > cutoff_delta:
                break
        selected.append(r)
        if ts is not None:
            prev_ts = ts
        if len(selected) >= MAX_CONVERSATION_TURNS:
            break

    selected.reverse()
    out: list[dict[str, Any]] = []
    for r in selected:
        slots = r.get("collected_slots") or {}
        digest = r.get("turn_digest") or []
        first_d = (
            digest[0]
            if (isinstance(digest, list) and digest and isinstance(digest[0], dict))
            else {}
        )
        step_receipts = [
            (s.get("result") or {}).get("receipt")
            for s in (r.get("steps") or [])
            if isinstance(s, dict) and (s.get("result") or {}).get("receipt")
        ]
        user_said = (
            first_d.get("user")
            or slots.get("user_message")
            or r.get("heard_transcript")
            or r.get("summary_title")
            or ""
        )
        assistant_replied = (
            first_d.get("assistant")
            or slots.get("assistant_reply")
            or ("; ".join(step_receipts) if step_receipts else None)
            or r.get("pending_prompt")
            or r.get("summary_title")
            or ""
        )
        out.append(
            {
                "goal_type": r.get("goal_type"),
                "status": r.get("status"),
                "user_said": str(user_said)[:240],
                "assistant_replied": str(assistant_replied)[:360],
                "project_id": r.get("project_id") or slots.get("project_id"),
                "customer_id": r.get("customer_id") or slots.get("customer_id"),
                "party_name": slots.get("party_name") or slots.get("full_name"),
            }
        )
    return out


def _safe_uuid_for_db(sb: Any, raw: Any, *, project_lookup: bool = False) -> str | None:
    """Return a Postgres-safe UUID string (or FakeSupabase ID in tests), resolving project codes when asked."""
    if raw is None:
        return None
    val = str(raw).strip()
    if not val or val.lower() in ("null", "none", "nil", "undefined", "n/a"):
        return None
    if hasattr(sb, "store") and not getattr(sb, "strict_uuids", False):
        return val
    try:
        return str(uuid.UUID(val))
    except ValueError:
        if project_lookup:
            code = val.lstrip("#").strip()
            rows = (
                sb.table("projects")
                .select("id")
                .ilike("project_code", code)
                .limit(1)
                .execute()
                .data
                or []
            )
            if rows and rows[0].get("id"):
                return str(rows[0]["id"])
        return None


def record_query_turn_in_history(
    sb: Any,
    *,
    owner_person_id: str | None,
    source_channel: str,
    domain_scope: str,
    telegram_chat_id: int | None,
    ui_context: dict[str, Any],
    thinking_level: str,
    user_text: str | None,
    heard_transcript: str | None,
    reply_text: str,
    referenced_project_id: str | None = None,
    referenced_customer_id: str | None = None,
) -> dict[str, Any] | None:
    """Persist completed Q&A / WhatsApp draft turns so follow-up turns retain context."""
    try:
        safe_owner = _safe_uuid_for_db(sb, owner_person_id)
        proj_id = _safe_uuid_for_db(sb, referenced_project_id, project_lookup=True)
        if not proj_id and ui_context.get("viewing_project_id"):
            proj_id = _safe_uuid_for_db(sb, ui_context["viewing_project_id"], project_lookup=True)
        cust_id = _safe_uuid_for_db(sb, referenced_customer_id)

        user_msg = (user_text or heard_transcript or "Question").strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "owner_person_id": safe_owner,
            "source_channel": source_channel,
            "domain_scope": domain_scope,
            "telegram_chat_id": telegram_chat_id,
            "ui_context": ui_context,
            "goal_type": "query",
            "status": "completed",
            "thinking_level_used": thinking_level,
            "heard_transcript": heard_transcript,
            "summary_title": f"Q: {user_msg[:170]}",
            "collected_slots": {
                "user_message": user_msg[:400],
                "assistant_reply": reply_text[:600],
                **({"project_id": proj_id} if proj_id else {}),
                **({"customer_id": cust_id} if cust_id else {}),
            },
            "missing_slots": [],
            "pending_prompt": None,
            "steps": [],
            "repair_options": [],
            "turn_digest": [
                {"user": user_msg[:400], "assistant": reply_text[:600], "at": now_iso}
            ],
            "project_id": proj_id,
            "customer_id": cust_id,
            "resolved_at": now_iso,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        res = sb.table("agent_goals").insert(payload).execute().data
        return res[0] if res else None
    except Exception as exc:
        logger.warning("Query history insert skipped safely: %s", exc)
        return None
        return None
