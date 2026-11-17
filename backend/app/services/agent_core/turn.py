"""Omnichannel AgentCore turn orchestration (Gemini 4 Hydrogen + Decisions triage + routing)."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import logging
import re
import sys
from typing import Any, Literal

from app.services import (
    action_tools, agent_tools, attachment_memory, decisions_triage, goal_intelligence, storage, turn_cache,
)
from app.services.agent_core.formatting import (
    _build_steps_for_slots, _default_missing_question, _format_confirmation_summary, _upsert_goal_row,
)
from app.services.agent_core.grounding import _build_compact_grounding, _build_system_instruction
from app.services.agent_core.schemas import AgentTurnEnvelope, GoalIntentItem, _strip_json_fences
from app.services.agent_core.slot_resolvers import (
    _accept_id, _backfill_financial_slots_from_text, _backfill_item_slots_from_title_and_text,
    _backfill_material_slots_from_text, _backfill_onboarding_and_open_goal_slots,
    _build_default_missing_slot_choices, _compute_missing_slots_for_op, _fetch_target_row_for_slots,
    _is_valid_uuid, _map_op_to_db_goal_type, _sanitize_and_resolve_slot_ids, parse_initial_items_slot,
)
from app.services.goal_primitives import (
    ThinkingLevel, has_complex_turn_cues, is_high_sensitivity_master_mutation, select_hydrogen_thinking_level,
)

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


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
    extra_audio_files: list[tuple[bytes, str]] | None = None,
    media_files: list[tuple[bytes, str]] | None = None,
    ui_context: dict[str, Any] | None = None,
    telegram_chat_id: int | None = None,
    reply_to_bot_msg_id: int | None = None,
    pinned_goal_id: str | None = None,
) -> dict[str, Any]:
    """Execute one complete omnichannel AgentCore turn and return structured UI/Telegram response."""
    pkg = _pkg()
    settings = pkg.get_settings()
    sb = turn_cache.wrap_turn_read_cache(sb)

    # 0. Restore recent unclaimed attachment (captionless hold or "above pdf" 15m memory)
    uploaded_gcs_paths: list[str] = []
    retired_bot_msg_ids: list[int] = []
    from_cached_attachments = False
    if not media_files and (text or audio_bytes or extra_audio_files):
        restored_files, restored_paths, retired_ids = attachment_memory.resolve_recent_unclaimed_attachments(
            sb,
            text=text,
            owner_person_id=owner_person_id,
            telegram_chat_id=telegram_chat_id,
            domain_scope=domain_scope,
        )
        if restored_files:
            media_files = restored_files
            uploaded_gcs_paths = restored_paths
            retired_bot_msg_ids = retired_ids
            from_cached_attachments = True

    grounding_json, acc_code_to_id = _build_compact_grounding(
        sb,
        owner_person_id=owner_person_id,
        domain_scope=domain_scope,
        ui_context=ui_context,
        source_channel=source_channel,
        telegram_chat_id=telegram_chat_id,
        reply_to_bot_msg_id=reply_to_bot_msg_id,
    )
    sys_inst = _build_system_instruction(grounding_json)
    open_goals_count = len(json.loads(grounding_json).get("open_goals_tray") or [])
    has_pdf = any(m == "application/pdf" for _, m in (media_files or []))

    # 1. Surgical Media Triage via gemini-decision-1 ($0 output) when photos/PDFs arrive
    thinking_level: ThinkingLevel = select_hydrogen_thinking_level(
        is_multi_item_dump=has_complex_turn_cues(
            text,
            has_pdf=has_pdf,
            open_goals_count=open_goals_count,
        )
    )
    media_triage_notes: list[str] = []
    triage_kinds: list[str] = []
    uploaded_mimes: list[str] = [m for _, m in (media_files or [])]

    if media_files and not from_cached_attachments:
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

            triage_kinds.append(triage.media_kind)
            ext = "pdf" if m_mime == "application/pdf" else "webp"
            folder = "supplier_bills" if triage.media_kind == "supplier_bill" else "projects/inbox"
            obj_path = f"{folder}/{now_ts}_{idx}.{ext}"
            storage._remember_local_blob(obj_path, m_bytes)
            uploaded_gcs_paths.append(obj_path)
            try:
                if ext == "webp":
                    storage.upload_image_as_webp(obj_path, m_bytes)
                else:
                    storage.upload_bytes(obj_path, m_bytes, content_type=m_mime)
            except Exception as exc:
                logger.warning("GCS upload skipped in local/dev mode: %s", exc)

            media_triage_notes.append(
                f"Attachment #{idx + 1}: classified={triage.media_kind}, ocr_difficulty={triage.ocr_difficulty:.2f}"
            )

        if attachment_memory.should_hold_captionless_upload(
            text=text,
            has_audio=bool(audio_bytes or extra_audio_files),
            media_kinds=triage_kinds,
            domain_scope=domain_scope,
        ):
            return attachment_memory.save_captionless_attachment_hold(
                sb,
                owner_person_id=owner_person_id,
                source_channel=source_channel,
                domain_scope=domain_scope,
                telegram_chat_id=telegram_chat_id,
                ui_context=ui_context,
                uploaded_gcs_paths=uploaded_gcs_paths,
                uploaded_mimes=uploaded_mimes,
            )

    # 2. Build Interactions API Multimodal Input
    all_audio_files: list[tuple[bytes, str]] = []
    if audio_bytes:
        all_audio_files.append((audio_bytes, audio_mime_type))
    for ex_bytes, ex_mime in extra_audio_files or []:
        if ex_bytes:
            all_audio_files.append((ex_bytes, ex_mime or audio_mime_type))

    input_blocks: list[dict[str, Any]] = []
    for a_bytes, a_mime in all_audio_files:
        input_blocks.append(
            {
                "type": "audio",
                "data": base64.b64encode(a_bytes).decode("ascii"),
                "mime_type": a_mime,
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
    if pinned_goal_id:
        prompt_parts.append(f"[PINNED OPEN GOAL TO RESOLVE: goal_id={pinned_goal_id}]")
    if text and text.strip():
        prompt_parts.append(text.strip())
    elif not prompt_parts:
        prompt_parts.append("Process the attached voice/media update accurately.")
    input_blocks.append({"type": "text", "text": "\n".join(prompt_parts)})

    client = pkg._get_interactions_client()

    # 3. Native Tool-Calling Loop with Stateful TurnDraftCart (Upgrade A + B + C; no JSON response_format!)
    cart = action_tools.TurnDraftCart()
    read_tools_called = 0

    def _extract_heard_and_text(raw: str) -> tuple[str | None, str]:
        clean = (raw or "").strip()
        if not clean:
            return None, ""
        lines = clean.splitlines()
        m_h = re.match(r"^\s*HEARD\s*:\s*(.+)$", lines[0], re.IGNORECASE)
        if m_h:
            return (m_h.group(1).strip().strip('"').strip("'") or None), "\n".join(lines[1:]).strip()
        return None, clean

    async def _run_tool_calls(fn_calls: list[Any], *, cur_heard: str | None) -> tuple[list[dict[str, Any]], bool]:
        nonlocal read_tools_called
        results: list[dict[str, Any]] = []
        all_staged_cleanly = bool(fn_calls)
        for fc in fn_calls:
            raw_args = getattr(fc, "arguments", None) or {}
            args_dict = raw_args if isinstance(raw_args, dict) else json.loads(str(raw_args))
            if action_tools.is_action_tool(fc.name):
                tool_out = await action_tools.execute_action_tool(
                    sb, fc.name, args_dict, cart=cart, acc_code_to_id=acc_code_to_id, ui_context=ui_context,
                    user_text=text, heard_transcript=cur_heard, reply_to_bot_msg_id=reply_to_bot_msg_id,
                    telegram_chat_id=telegram_chat_id, pinned_goal_id=pinned_goal_id,
                )
                try:
                    parsed_out = json.loads(tool_out)
                    if not (parsed_out.get("ok") and parsed_out.get("staged")):
                        all_staged_cleanly = False
                except Exception:
                    all_staged_cleanly = False
            else:
                all_staged_cleanly = False
                read_tools_called += 1
                tool_out = await agent_tools.execute_read_tool(sb, fc.name, args_dict)
            results.append({"type": "function_result", "call_id": fc.id, "name": fc.name, "result": [{"type": "text", "text": tool_out}]})
        return results, all_staged_cleanly

    interaction = await asyncio.to_thread(
        client.interactions.create,
        model=settings.gemini_live_model,
        system_instruction=sys_inst,
        input=input_blocks,
        tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
        generation_config={"thinking_level": thinking_level},
    )

    has_question_cue = "?" in (text or "") or bool(
        re.search(r"\b(kitna|kitne|kaun|kya|balance|batao|dikhao|how much|what is)\b", (text or ""), re.I)
    )
    for _round in range(4):
        fn_calls = [s for s in (getattr(interaction, "steps", None) or []) if getattr(s, "type", None) == "function_call"]
        if not fn_calls:
            break
        fn_results, all_staged_cleanly = await _run_tool_calls(fn_calls, cur_heard=None)
        audio_needs_transcript = bool(all_audio_files) and not bool(cart.heard_transcript)
        if all_staged_cleanly and cart.has_items() and not has_question_cue and not audio_needs_transcript:
            break
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=settings.gemini_live_model,
            previous_interaction_id=interaction.id,
            system_instruction=sys_inst,
            input=fn_results,
            tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
            generation_config={"thinking_level": thinking_level},
        )

    raw_out = (getattr(interaction, "output_text", None) or "").strip()
    stripped_out = _strip_json_fences(raw_out)

    def _merge_legacy_items(cur_env: list[GoalIntentItem]) -> list[GoalIntentItem]:
        by_key: dict[str, tuple[GoalIntentItem, int]] = {}
        order: list[str] = []
        for cand in cur_env:
            missing, resolved_slots = action_tools.preflight_goal_item_missing_slots(
                sb,
                cand,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=text,
                heard_transcript=heard,
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if not cand.party_name and resolved_slots.get("party_name"):
                cand.party_name = str(resolved_slots["party_name"])
            key = action_tools.goal_item_dedup_key(cand, resolved_slots)
            miss_cnt = len(missing)
            if key not in by_key or miss_cnt < by_key[key][1]:
                if key not in by_key:
                    order.append(key)
                by_key[key] = (cand, miss_cnt)
        return [by_key[k][0] for k in order]

    envelope = AgentTurnEnvelope(intent_category="goal_actions", goal_items=[])
    merged_items: list[GoalIntentItem] = []
    clarified_keys: set[str] = set()
    heard: str | None = None

    if cart.has_items():
        # Primary Upgrade A+C Path: action tools exclusively own goal state; ignore any text/JSON echo!
        parsed_heard, _ = _extract_heard_and_text(raw_out)
        heard = (cart.heard_transcript or parsed_heard) if all_audio_files else None
        merged_items = cart.items()
        clarified_keys = cart.clarified_keys()
    elif stripped_out.startswith("{"):
        # Compatibility path for JSON-mocked unit tests & legacy structured envelopes
        envelope = AgentTurnEnvelope.model_validate_json(stripped_out)
        if envelope.intent_category == "goal_actions" and not envelope.goal_items:
            try:
                retry_int = await asyncio.to_thread(
                    client.interactions.create,
                    model=settings.gemini_live_model,
                    previous_interaction_id=getattr(interaction, "id", None),
                    system_instruction=sys_inst,
                    input=[
                        {
                            "type": "text",
                            "text": (
                                "You set intent_category='goal_actions' without calling an action tool or populating goal_items. "
                                "Call the appropriate action tool now, or answer plainly in direct_reply_text."
                            ),
                        }
                    ],
                    tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
                    generation_config={"thinking_level": thinking_level},
                )
                interaction = retry_int
                r_calls = [s for s in (getattr(retry_int, "steps", None) or []) if getattr(s, "type", None) == "function_call"]
                if r_calls:
                    await _run_tool_calls(r_calls, cur_heard=None)
                retry_txt = _strip_json_fences(getattr(retry_int, "output_text", None) or "")
                if retry_txt.startswith("{"):
                    envelope = AgentTurnEnvelope.model_validate_json(retry_txt)
            except Exception as exc:
                logger.warning("Empty goal_items retry failed safely: %s", exc)
        heard = (cart.heard_transcript or envelope.heard_transcript) if all_audio_files else None
        merged_items = cart.items() if cart.has_items() else _merge_legacy_items(list(envelope.goal_items or []))
        clarified_keys = cart.clarified_keys()
    else:
        # Native plain-text reply / Missing-tool nudge guard
        parsed_heard, plain_text = _extract_heard_and_text(raw_out)
        heard = (cart.heard_transcript or parsed_heard) if all_audio_files else None
        txt_clean = (text or "").strip()
        is_query = (
            "?" in txt_clean
            or "whatsapp" in txt_clean.lower()
            or bool(
                re.search(
                    r"^(who|what|which|when|where|how|why|kitna|kitne|kaun|kya|batao|bata|dikhao|show|list|draft|write)\b",
                    txt_clean,
                    re.I,
                )
            )
        )
        claimed_action = bool(
            re.search(r"\b(saved|staged|moved|added|updated|recorded|posted|kar diya|ho gaya|noted)\b", plain_text, re.I)
        )
        if txt_clean and read_tools_called == 0 and (not is_query or pinned_goal_id is not None) and (not plain_text or claimed_action):
            try:
                nudge_int = await asyncio.to_thread(
                    client.interactions.create,
                    model=settings.gemini_live_model,
                    previous_interaction_id=getattr(interaction, "id", None),
                    system_instruction=sys_inst,
                    input=[
                        {
                            "type": "text",
                            "text": (
                                "Nothing was staged yet because no action tool was called. "
                                "If the user requested a create/update/move/payment, call the appropriate action tool now "
                                "(or resolve_open_goal). Otherwise give a helpful direct answer."
                            ),
                        }
                    ],
                    tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
                    generation_config={"thinking_level": thinking_level},
                )
                n_calls = [s for s in (getattr(nudge_int, "steps", None) or []) if getattr(s, "type", None) == "function_call"]
                if n_calls:
                    await _run_tool_calls(n_calls, cur_heard=heard)
                    if cart.has_items():
                        merged_items = cart.items()
                        clarified_keys = cart.clarified_keys()
                if not merged_items:
                    n_heard, n_plain = _extract_heard_and_text(getattr(nudge_int, "output_text", None) or "")
                    if n_plain:
                        plain_text = n_plain
                    if n_heard and all_audio_files and not heard:
                        heard = n_heard
            except Exception as exc:
                logger.warning("Missing-tool action nudge failed safely: %s", exc)

        if not merged_items:
            if not plain_text:
                return {
                    "kind": "unrecognized",
                    "is_error": True,
                    "heard_transcript": heard,
                    "reply_text": "⚠️ Could not match that command to an order or action — please check the customer/order name and try again.",
                    "goals": [],
                    "parked_goal_nudge": None,
                }
            envelope = AgentTurnEnvelope(intent_category="direct_answer", direct_reply_text=plain_text, goal_items=[])

    # 3b. Silent Internal Self-Repair before asking user for already-stated details
    if merged_items:
        unconfirmed_missing: list[dict[str, Any]] = []
        has_open_target = reply_to_bot_msg_id is not None or pinned_goal_id is not None
        for idx_m, cand in enumerate(merged_items):
            if cand.target_open_goal_id:
                has_open_target = True
            if cand.is_ambiguous or action_tools.goal_item_dedup_key(cand) in clarified_keys:
                continue
            missing_m, _ = action_tools.preflight_goal_item_missing_slots(
                sb,
                cand,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=text,
                heard_transcript=heard,
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if missing_m:
                draft_lbl = next((did for did, ent in cart.entries.items() if ent["item"] is cand), f"D{idx_m + 1}")
                unconfirmed_missing.append(
                    {
                        "draft_id": draft_lbl,
                        "index": idx_m,
                        "operation_kind": cand.operation_kind,
                        "summary_title": cand.summary_title,
                        "missing_slots": missing_m,
                    }
                )

        if unconfirmed_missing:
            txt_clean = (text or "").strip()
            low_clean = txt_clean.lower()
            critical_slot = any(
                slot in {"item_name", "material_lines_json", "rough_scope", "full_name"}
                for entry in unconfirmed_missing
                for slot in entry["missing_slots"]
            )
            pushback_cues = ("already", "confus", "these are", "bataya", "likha", "above", "upar", "nahi", "not expense")
            should_repair = (
                (critical_slot and len(txt_clean) >= 15)
                or bool(media_files)
                or len(txt_clean) >= 40
                or any(sep in (text or "") for sep in (";", "+", "\n", ":"))
                or has_open_target
                or any(cue in low_clean for cue in pushback_cues)
            )
            if should_repair:
                repair_thinking: ThinkingLevel = "high" if thinking_level == "high" else "medium"
                repair_prompt = (
                    "INTERNAL PRE-FLIGHT CHECK (user has NOT seen this yet): "
                    "Python rejected draft(s) due to missing required fields:\n"
                    f"{json.dumps(unconfirmed_missing, ensure_ascii=False)}\n\n"
                    "Carefully re-read the user's message, attachment/PDF, and CONTEXT. "
                    "Re-call the action tool with `draft_id` (or populate all missing fields cleanly) if stated or implied. "
                    "Only pass `allow_missing_clarification=true` with `clarification_question` if genuinely absent."
                )
                try:
                    repair_int = await asyncio.to_thread(
                        client.interactions.create,
                        model=settings.gemini_live_model,
                        previous_interaction_id=getattr(interaction, "id", None),
                        system_instruction=sys_inst,
                        input=[{"type": "text", "text": repair_prompt}],
                        tools=agent_tools.INTERACTION_TOOLS_SCHEMA,
                        generation_config={"thinking_level": repair_thinking},
                    )
                    rep_fn_calls = [
                        s for s in (getattr(repair_int, "steps", None) or [])
                        if getattr(s, "type", None) == "function_call"
                    ]
                    if rep_fn_calls:
                        await _run_tool_calls(rep_fn_calls, cur_heard=heard)
                    if cart.has_items():
                        merged_items = cart.items()
                        clarified_keys = cart.clarified_keys()
                    else:
                        rep_raw = _strip_json_fences(getattr(repair_int, "output_text", None) or "")
                        if rep_raw.startswith("{"):
                            rep_env = AgentTurnEnvelope.model_validate_json(rep_raw)
                            repaired_merged = _merge_legacy_items(list(rep_env.goal_items or []))
                            if repaired_merged:
                                merged_items = repaired_merged
                            if rep_env.direct_reply_text:
                                envelope.direct_reply_text = rep_env.direct_reply_text
                except Exception as exc:
                    logger.warning("Silent self-repair pass failed safely: %s", exc)

    if envelope.intent_category in ("direct_answer", "draft_whatsapp_message") and not merged_items:
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

    if not merged_items:
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
    for item in merged_items:
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
            reply_to_bot_msg_id=reply_to_bot_msg_id,
            pinned_goal_id=pinned_goal_id,
        )
        processed_goals.append(g_out)

    parked_nudge: dict[str, Any] | None = None
    if processed_goals and all(g.get("status") == "completed" for g in processed_goals):
        touched = {
            str(g["goal_id"]) for g in processed_goals if g.get("goal_id")
        } | {
            str(it.target_open_goal_id)
            for it in merged_items
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
        "retired_bot_msg_ids": retired_bot_msg_ids,
    }


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
    reply_to_bot_msg_id: int | None = None,
    pinned_goal_id: str | None = None,
) -> dict[str, Any]:
    pkg = _pkg()
    prior_slots: dict[str, Any] = {}
    prior_summary = ""
    prior_missing_slots: list[str] = []
    existing_goal_id: str | None = None
    existing_owner_person_id: str | None = None
    prior_bot_msg_id: int | None = None

    effective_open_id = item.target_open_goal_id or pinned_goal_id
    if effective_open_id and _accept_id(sb, effective_open_id, "agent_goals"):
        g_rows = (
            sb.table("agent_goals")
            .select("*")
            .eq("id", effective_open_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if g_rows:
            existing_goal_id = g_rows[0]["id"]
            existing_owner_person_id = g_rows[0].get("owner_person_id")
            prior_slots = dict(g_rows[0].get("collected_slots") or {})
            prior_summary = str(g_rows[0].get("summary_title") or "")
            prior_missing_slots = list(g_rows[0].get("missing_slots") or [])
            prior_bot_msg_id = g_rows[0].get("telegram_bot_msg_id")

    if not existing_goal_id and reply_to_bot_msg_id is not None:
        pinned_rows = (
            sb.table("agent_goals")
            .select("*")
            .in_("status", ["gathering_info", "awaiting_confirmation", "needs_error_fix"])
            .eq("telegram_bot_msg_id", int(reply_to_bot_msg_id))
            .execute()
            .data
            or []
        )
        chat_pinned = [
            r
            for r in pinned_rows
            if telegram_chat_id is None or r.get("telegram_chat_id") in (None, telegram_chat_id)
        ]
        expected_gtype = _map_op_to_db_goal_type(item.operation_kind)
        matched_pinned = [r for r in chat_pinned if r.get("goal_type") == expected_gtype]
        if not matched_pinned and len(chat_pinned) == 1:
            matched_pinned = chat_pinned
        if len(matched_pinned) == 1:
            g_pin = matched_pinned[0]
            existing_goal_id = g_pin["id"]
            existing_owner_person_id = g_pin.get("owner_person_id")
            prior_slots = dict(g_pin.get("collected_slots") or {})
            prior_summary = str(g_pin.get("summary_title") or "")
            prior_missing_slots = list(g_pin.get("missing_slots") or [])
            prior_bot_msg_id = g_pin.get("telegram_bot_msg_id")

    # Merge non-null slots from current turn over prior_slots
    new_slots = {
        k: v
        for k, v in item.model_dump(
            exclude={"target_open_goal_id", "is_ambiguous", "clarification_question", "choices"}
        ).items()
        if v is not None and v != [] and v != ""
    }
    slots: dict[str, Any] = {**prior_slots, **new_slots}
    if slots.get("zoho_contact_id") and (
        _is_valid_uuid(slots["zoho_contact_id"])
        or str(slots["zoho_contact_id"]).strip().lower() in ("none", "none_needed", "null", "na", "n/a")
    ):
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
    _backfill_item_slots_from_title_and_text(
        slots,
        summary_title=item.summary_title,
        user_text=user_text,
        heard_transcript=heard_transcript,
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
    if str(slots.get("operation_kind") or item.operation_kind) in ("new_lead", "update_order_details"):
        parse_initial_items_slot(slots)

    # Resolve payment_account_code -> payment_account_id
    if slots.get("payment_account_code") and slots["payment_account_code"] in acc_code_to_id:
        slots["payment_account_id"] = acc_code_to_id[slots["payment_account_code"]]
    if slots.get("to_payment_account_code") and slots["to_payment_account_code"] in acc_code_to_id:
        slots["to_payment_account_id"] = acc_code_to_id[slots["to_payment_account_code"]]

    op = str(slots.get("operation_kind") or item.operation_kind)
    goal_owner_id = existing_owner_person_id or owner_person_id

    missing = _compute_missing_slots_for_op(op, slots)
    choices_serialized = [c.model_dump() for c in item.choices]

    # Case A: Missing slots or Homonym Ambiguity -> status = 'gathering_info'
    if item.is_ambiguous or missing:
        if not choices_serialized and not item.is_ambiguous and len(missing) == 1:
            choices_serialized = _build_default_missing_slot_choices(sb, missing[0], op)
        prompt_q = item.clarification_question or _default_missing_question(op, missing)
        goal_row = _upsert_goal_row(
            sb,
            goal_id=existing_goal_id,
            owner_person_id=goal_owner_id,
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
            **({"superseded_bot_msg_id": prior_bot_msg_id} if prior_bot_msg_id else {}),
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
            owner_person_id=goal_owner_id,
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
        res_b = await pkg.confirm_and_execute_goal(
            sb,
            goal_id=str(goal_row["id"]),
            actor_person_id=owner_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
        )
        if prior_bot_msg_id:
            res_b["superseded_bot_msg_id"] = prior_bot_msg_id
        return res_b

    # Case C: Money, Zoho Draft Bills/Invoices, New Entities, Soft-Archives & High-Sensitivity Edits -> MANDATORY CONFIRM!
    confirm_card_text = _format_confirmation_summary(op, slots, target_before_row)
    steps = _build_steps_for_slots(op, slots)
    goal_row = _upsert_goal_row(
        sb,
        goal_id=existing_goal_id,
        owner_person_id=goal_owner_id,
        source_channel=source_channel,
        domain_scope="munim" if op in ("customer_receipt", "customer_payment", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill", "customer_draft_invoice") else domain_scope,
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
        **({"superseded_bot_msg_id": prior_bot_msg_id} if prior_bot_msg_id else {}),
    }
