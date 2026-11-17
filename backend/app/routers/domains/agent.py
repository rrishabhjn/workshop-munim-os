"""Omnichannel AgentCore Endpoints (`/api/agent/*`)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import CurrentUser, get_current_user
from .common import (
    GoalChoiceRequest,
    GoalEditRequest,
    GoalReplyRequest,
    _pkg,
    _resolve_actor_access,
    _resolve_actor_person,
    _validate_goal_id_or_400,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/agent/needs-info")
async def list_needs_info_queue(
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _ = user
    sb = _pkg().get_supabase_admin()
    return sb.table("v_needs_info_queue").select("*").execute().data or []


@router.post("/agent/turn")
async def execute_web_agent_turn(
    text: str = Form(default=""),
    domain_scope: str = Form(default="production"),
    ui_context_json: str = Form(default="{}"),
    audio: UploadFile | None = File(default=None),
    attachment: UploadFile | None = File(default=None),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, actor_name, can_munim = _resolve_actor_access(sb, user)
    scope = "munim" if domain_scope == "munim" else "production"
    if scope == "munim" and not can_munim:
        return {
            "kind": "error",
            "is_error": True,
            "reply_text": "🔒 Munim finance assistant access is disabled for your account. Please switch to Production.",
            "goals": [],
        }

    try:
        ui_ctx = json.loads(ui_context_json) if ui_context_json else {}
    except json.JSONDecodeError:
        ui_ctx = {}

    audio_bytes: bytes | None = None
    audio_mime = "audio/webm"
    if audio is not None:
        audio_bytes = await audio.read()
        audio_mime = audio.content_type or "audio/webm"

    media_files: list[tuple[bytes, str]] = []
    if attachment is not None:
        att_bytes = await attachment.read()
        att_mime = attachment.content_type or "image/jpeg"
        media_files.append((att_bytes, att_mime))

    try:
        return await pkg.agent_core.run_agent_turn(
            sb,
            owner_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web_command_bar",
            domain_scope=scope,
            text=text or None,
            audio_bytes=audio_bytes,
            audio_mime_type=audio_mime,
            media_files=media_files or None,
            ui_context=ui_ctx,
        )
    except Exception as exc:
        logger.exception("Unhandled error in /api/agent/turn: %s", exc)
        return {
            "kind": "error",
            "is_error": True,
            "reply_text": "⚠️ Could not process that command cleanly — your text is preserved below so you can adjust or retry.",
            "goals": [],
        }


@router.post("/agent/goals/{goal_id}/confirm")
async def confirm_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, actor_name = _resolve_actor_person(sb, user)
    try:
        return await pkg.agent_core.confirm_and_execute_goal(
            sb,
            goal_id=gid,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web",
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/confirm: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not execute goal safely.") from exc


@router.post("/agent/goals/{goal_id}/choice")
async def choose_agent_goal_option(
    goal_id: str,
    body: GoalChoiceRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, actor_name = _resolve_actor_person(sb, user)
    try:
        return await pkg.agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=gid,
            slot_patch=body.slot_patch,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web",
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/choice: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not apply option safely.") from exc


@router.post("/agent/goals/{goal_id}/undo")
async def undo_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, _ = _resolve_actor_person(sb, user)
    try:
        return pkg.agent_core.undo_completed_goal(sb, goal_id=gid, actor_person_id=actor_id)
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/undo: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not undo action safely.") from exc


@router.post("/agent/goals/{goal_id}/park")
async def park_agent_goal_for_clerk(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    return pkg.agent_core.park_goal_for_clerk(sb, gid)


@router.post("/agent/goals/{goal_id}/discard")
async def discard_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    return pkg.agent_core.discard_goal(sb, gid)


@router.post("/agent/goals/{goal_id}/edit")
async def edit_agent_goal(
    goal_id: str,
    body: GoalEditRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    try:
        return await asyncio.to_thread(
            pkg.agent_core.edit_open_goal_slots,
            sb,
            goal_id=gid,
            slot_patch=body.slot_patch,
            correction_note=body.correction_note,
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/edit: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not update goal edits cleanly.") from exc


@router.post("/agent/goals/{goal_id}/reply")
async def reply_to_agent_goal(
    goal_id: str,
    body: GoalReplyRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, actor_name, _ = _resolve_actor_access(sb, user)
    rows = sb.table("agent_goals").select("*").eq("id", gid).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Open goal not found.")
    goal_row = rows[0]
    reply_txt = body.text.strip()
    if not reply_txt:
        raise HTTPException(status_code=400, detail="Reply text cannot be empty.")

    missing_slots = list(goal_row.get("missing_slots") or [])
    if goal_row.get("status") == "gathering_info" and len(missing_slots) == 1:
        slot = str(missing_slots[0])
        slot_patch: dict[str, Any] = {}
        if slot == "party_name":
            m = await pkg.zoho.match_existing_payee(sb, reply_txt)
            if m.get("status") == "matched":
                slot_patch = {
                    "party_name": m["canonical_name"],
                    "is_new_payee": False,
                    **({"zoho_contact_id": m["zoho_contact_id"]} if m.get("zoho_contact_id") else {}),
                    **({"vendor_id": m["vendor_id"]} if m.get("vendor_id") else {}),
                    **({"person_id": m["person_id"]} if m.get("person_id") else {}),
                }
            elif m.get("status") == "new":
                slot_patch = {"party_name": reply_txt, "is_new_payee": True}
            elif m.get("status") == "ambiguous":
                sb.table("agent_goals").update(
                    {"pending_prompt": m.get("question"), "repair_options": m.get("choices") or []}
                ).eq("id", gid).execute()
                return {
                    "kind": "goal_actions",
                    "goals": [
                        {
                            "goal_id": gid,
                            "status": "gathering_info",
                            "summary_title": goal_row.get("summary_title"),
                            "prompt": m.get("question"),
                            "choices": m.get("choices") or [],
                            "missing_slots": missing_slots,
                            "collected_slots": goal_row.get("collected_slots") or {},
                        }
                    ],
                }
        elif slot in ("area_landmark", "rough_scope", "full_name", "item_name", "invoice_no"):
            slot_patch = {slot: reply_txt}
        elif slot == "amount_inr":
            amt = pkg.agent_core._extract_inr_amount_from_text(reply_txt)
            if amt is not None:
                slot_patch = {"amount_inr": amt}
        elif slot == "payment_account_code":
            code = pkg.agent_core._extract_payment_account_code_from_text(sb, reply_txt)
            if code:
                slot_patch = {"payment_account_code": code}

        if slot_patch:
            res = await pkg.agent_core.apply_slot_patch_and_resume(
                sb,
                goal_id=gid,
                slot_patch=slot_patch,
                actor_person_id=actor_id,
                actor_label=actor_name,
                source_channel="web",
            )
            return {"kind": "goal_actions", "goals": [res]}

    return await pkg.agent_core.run_agent_turn(
        sb,
        owner_person_id=actor_id,
        actor_label=actor_name,
        source_channel="web_command_bar",
        domain_scope="munim" if goal_row.get("domain_scope") == "munim" else "production",
        text=reply_txt,
        ui_context=goal_row.get("ui_context") or {},
        pinned_goal_id=gid,
    )
