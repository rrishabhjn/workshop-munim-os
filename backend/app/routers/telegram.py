"""Hardened 3-Channel Telegram Webhook & 2.5s Burst-Debounced AgentCore Worker.

Enforces:
  - Secret-token header verification + allowlisted telegram_user_id check
  - Webhook idempotency via `telegram_webhook_dedup`
  - 2.5s burst batching via `telegram_inbox_buffer` + per-(chat_id, user_id) claim
  - Surgical `gemini-decision-1` relevance check on unaddressed group messages
  - Omnichannel `AgentCore` execution (`[↩️ Undo]` for Production, `[✅ Confirm]` for Munim,
    1-tap disambiguation/repair buttons + `[📥 Let Clerk Fix Later]`)
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hmac
import json
import logging
from typing import Any
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db import get_supabase_admin
from app.services import agent_core, decisions_triage, goal_intelligence, telegram_api
from app.services.goal_primitives import (
    format_voice_receipt_header,
    merge_buffered_telegram_burst,
    should_run_decisions_front_door,
)

router = APIRouter(tags=["telegram"])
logger = logging.getLogger(__name__)
_BURST_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_STALE_CLAIM_SECONDS = 120


def _get_burst_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (int(chat_id), int(user_id))
    if key not in _BURST_LOCKS:
        _BURST_LOCKS[key] = asyncio.Lock()
    return _BURST_LOCKS[key]


def _parse_iso_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Preserved Helpers (Used by Legacy/Security Unit Tests)
# ---------------------------------------------------------------------------
_VALID_TXN_TYPES = {
    "cash_advance",
    "cash_balance",
    "customer_receipt",
    "vendor_payment",
    "karigar_labor",
    "local_transport",
    "overhead_expense",
    "misc",
}

_TXN_TYPE_ALIASES = {
    "customer_payment": "customer_receipt",
    "customer_advance": "customer_receipt",
    "supplier_payment": "vendor_payment",
    "vendor": "vendor_payment",
    "salary": "karigar_labor",
    "wages": "karigar_labor",
    "employee": "karigar_labor",
    "transport": "local_transport",
    "logistics": "local_transport",
    "rent": "overhead_expense",
    "emi": "overhead_expense",
    "electricity": "overhead_expense",
    "water": "overhead_expense",
    "fuel": "overhead_expense",
    "interest": "overhead_expense",
    "utility": "overhead_expense",
}


def _normalize_txn_type(raw: str | None, direction: str) -> str:
    val = (raw or "").strip().lower()
    if val in _VALID_TXN_TYPES:
        return val
    if val in _TXN_TYPE_ALIASES:
        return _TXN_TYPE_ALIASES[val]
    return "customer_receipt" if direction == "in" else "misc"


def _is_state_expired(state_row: dict[str, Any], max_age_minutes: int = 360) -> bool:
    created_raw = state_row.get("created_at")
    if not created_raw:
        return False
    try:
        created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).total_seconds() > max_age_minutes * 60
    except ValueError:
        return False


def _build_confirm_keyboard(state_id: str, actions: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    needs_pick = any(
        a.get("action_type") == "log_cash" and a.get("needs_txn_clarification")
        for a in actions
    )
    if needs_pick:
        kb = {
            "inline_keyboard": [
                [
                    {"text": "👤 Customer Receipt", "callback_data": f"txntype:{state_id}:customer_receipt"},
                    {"text": "🏭 Vendor / Supplier", "callback_data": f"txntype:{state_id}:vendor_payment"},
                ],
                [
                    {"text": "👷 Employee / Karigar", "callback_data": f"txntype:{state_id}:karigar_labor"},
                    {"text": "🚚 Transport", "callback_data": f"txntype:{state_id}:local_transport"},
                ],
                [
                    {"text": "🏠 Rent / Utility / EMI", "callback_data": f"txntype:{state_id}:overhead_expense"},
                    {"text": "❌ Cancel", "callback_data": f"cancel:{state_id}"},
                ],
            ]
        }
        return kb, True
    kb = {
        "inline_keyboard": [[
            {"text": "✅ Confirm All", "callback_data": f"confirm:{state_id}"},
            {"text": "✏️ Edit", "callback_data": f"edit:{state_id}"},
            {"text": "❌ Cancel", "callback_data": f"cancel:{state_id}"},
        ]]
    }
    return kb, False


def _build_contact_picker_keyboard(
    state_id: str, party_name: str, matches: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = [
        [{"text": f"✓ {m.get('contact_name') or m.get('name')}", "callback_data": f"pickcontact:{state_id}:{idx}"}]
        for idx, m in enumerate(matches[:4])
    ]
    rows.append([{"text": f"➕ Create '{party_name[:24]}' as New", "callback_data": f"pickcontact:{state_id}:new"}])
    rows.append([{"text": "❌ Cancel", "callback_data": f"cancel:{state_id}"}])
    return {"inline_keyboard": rows}


def _verify_webhook_secret(secret_header: str | None, expected: str) -> None:
    if expected and not hmac.compare_digest(secret_header or "", expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Telegram webhook secret token.",
        )


def _extract_user_and_chat(update: dict[str, Any]) -> tuple[int | None, int | None]:
    if "callback_query" in update:
        cq = update["callback_query"]
        user_id = cq.get("from", {}).get("id")
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        return user_id, chat_id
    msg = update.get("message") or update.get("edited_message") or {}
    user_id = msg.get("from", {}).get("id")
    chat_id = msg.get("chat", {}).get("id")
    return user_id, chat_id


def _resolve_domain_channel(chat_id: int | str, settings: Settings) -> str:
    cid = str(chat_id).strip()
    if settings.telegram_attendance_chat_id and cid == settings.telegram_attendance_chat_id.strip():
        return "attendance"
    if settings.effective_production_chat_id and cid == settings.effective_production_chat_id:
        return "production"
    if settings.effective_munim_chat_id and cid == settings.effective_munim_chat_id:
        return "munim"
    return "dm"


def _resolve_person_by_telegram(sb: Any, telegram_user_id: int | None, fallback_name: str) -> tuple[str | None, str]:
    if not telegram_user_id:
        return None, fallback_name
    rows = (
        sb.table("people")
        .select("id,full_name")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if rows:
        return str(rows[0]["id"]), str(rows[0]["full_name"])
    return None, fallback_name


def _dispatch_to_cloud_tasks(
    update: dict[str, Any], settings: Settings, delay_seconds: float = 0.0
) -> None:
    """Enqueue an async HTTP task via Cloud Tasks v2 REST API (zero grpcio dependency)."""
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
    target_url = f"{settings.app_base_url.rstrip('/')}/tasks/telegram-worker"
    body_b64 = base64.b64encode(json.dumps(update).encode("utf-8")).decode("ascii")
    task_obj: dict[str, Any] = {
        "httpRequest": {
            "httpMethod": "POST",
            "url": target_url,
            "headers": {
                "Content-Type": "application/json",
                "X-Cron-Secret": settings.cron_auth_secret,
            },
            "body": body_b64,
        }
    }
    if delay_seconds > 0:
        sched_dt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        task_obj["scheduleTime"] = sched_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    resp = httpx.post(
        api_url,
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"task": task_obj},
        timeout=10.0,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Inline Keyboards for AgentCore Goal States
# ---------------------------------------------------------------------------
def _build_goal_keyboard(goal_out: dict[str, Any]) -> dict[str, Any] | None:
    gid = str(goal_out.get("goal_id") or "")
    st = goal_out.get("status")
    choices = goal_out.get("choices") or []

    if st == "completed":
        if goal_out.get("undo_available"):
            return {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"ag_undo:{gid}"}]]}
        return None

    if st in ("awaiting_confirmation", "transient_retry_pending"):
        return {
            "inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"ag_confirm:{gid}"},
                {"text": "📥 Clerk Later", "callback_data": f"ag_park:{gid}"},
                {"text": "❌ Discard", "callback_data": f"ag_discard:{gid}"},
            ]]
        }

    if st in ("gathering_info", "needs_error_fix"):
        rows: list[list[dict[str, str]]] = []
        for idx, ch in enumerate(choices[:4]):
            rows.append([{"text": str(ch.get("label") or f"Option {idx + 1}")[:48], "callback_data": f"ag_choice:{gid}:{idx}"}])
        bottom_row = []
        if st == "needs_error_fix":
            bottom_row.append({"text": "📥 Let Clerk Fix Later", "callback_data": f"ag_park:{gid}"})
        bottom_row.append({"text": "❌ Discard", "callback_data": f"ag_discard:{gid}"})
        rows.append(bottom_row)
        return {"inline_keyboard": rows}

    return None


async def _handle_agent_callback(
    sb: Any, cq: dict[str, Any]
) -> dict[str, Any]:
    cq_id = cq["id"]
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    from_user = cq.get("from") or {}
    actor_id, actor_name = _resolve_person_by_telegram(
        sb, from_user.get("id"), from_user.get("first_name") or "Operator"
    )

    parts = data.split(":")
    cmd = parts[0]
    goal_id = parts[1] if len(parts) > 1 else ""

    if cmd == "ag_confirm":
        await telegram_api.answer_callback_query(cq_id, "Saving...")
        out = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=goal_id,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="telegram",
        )
        if out.get("status") == "completed":
            kb = _build_goal_keyboard(out) or {"inline_keyboard": []}
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ *Saved*\n{out.get('receipt') or ''}",
                reply_markup=kb,
            )
            return {"ok": True}

        kb = _build_goal_keyboard(out) or {"inline_keyboard": []}
        await telegram_api.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=out.get("prompt") or "Needs attention.",
            reply_markup=kb,
        )
        return {"ok": True}

    if cmd == "ag_undo":
        res = agent_core.undo_completed_goal(sb, goal_id=goal_id, actor_person_id=actor_id)
        summary = ", ".join(res.get("reverted") or []) or "Reverted."
        await telegram_api.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"↩️ *Undone* — {summary}",
            reply_markup={"inline_keyboard": []},
        )
        await telegram_api.answer_callback_query(cq_id, "Undone!")
        return {"ok": True}

    if cmd == "ag_park":
        agent_core.park_goal_for_clerk(sb, goal_id)
        await telegram_api.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="📥 *Parked in 'Needs Info' Queue* for Clerk review.",
            reply_markup={"inline_keyboard": []},
        )
        await telegram_api.answer_callback_query(cq_id, "Parked for Clerk")
        return {"ok": True}

    if cmd == "ag_discard":
        agent_core.discard_goal(sb, goal_id)
        await telegram_api.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="❌ *Discarded* — nothing was saved.",
            reply_markup={"inline_keyboard": []},
        )
        await telegram_api.answer_callback_query(cq_id, "Discarded")
        return {"ok": True}

    if cmd == "ag_choice" and len(parts) >= 3:
        await telegram_api.answer_callback_query(cq_id, "Applying...")
        choice_idx = int(parts[2])
        g_rows = sb.table("agent_goals").select("repair_options").eq("id", goal_id).limit(1).execute().data or []
        opts = (g_rows[0].get("repair_options") or []) if g_rows else []
        patch: dict[str, Any] = {}
        if 0 <= choice_idx < len(opts):
            raw_patch = opts[choice_idx].get("slot_patch_json") or "{}"
            try:
                patch = json.loads(raw_patch) if isinstance(raw_patch, str) else dict(raw_patch)
            except json.JSONDecodeError:
                patch = {}
        out = await agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=goal_id,
            slot_patch=patch,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="telegram",
        )
        kb = _build_goal_keyboard(out) or {"inline_keyboard": []}
        txt = f"✅ *Saved*\n{out.get('receipt')}" if out.get("status") == "completed" else (out.get("prompt") or "Updated.")
        await telegram_api.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=txt,
            reply_markup=kb,
        )
        return {"ok": True}

    await telegram_api.answer_callback_query(cq_id, "Action processed.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 2.5s Debounced Burst Worker
# ---------------------------------------------------------------------------
async def process_debounced_burst(
    chat_id: int, user_id: int, wait_locally: bool = False
) -> None:
    settings = get_settings()
    debounce_s = max(float(settings.telegram_burst_debounce_seconds or 0.0), 0.0)
    if wait_locally and debounce_s > 0:
        await asyncio.sleep(debounce_s)

    sb = get_supabase_admin()
    claimed_rows: list[dict[str, Any]] = []
    async with _get_burst_lock(chat_id, user_id):
        for attempt in range(3):
            now_utc = datetime.now(timezone.utc)
            active_rows = (
                sb.table("telegram_inbox_buffer")
                .select("*")
                .eq("chat_id", chat_id)
                .eq("user_id", user_id)
                .in_("status", ["buffered", "claimed"])
                .execute()
                .data
                or []
            )
            if not active_rows:
                return

            stale_cutoff = now_utc - timedelta(seconds=_STALE_CLAIM_SECONDS)
            recovered_ids: set[str] = set()
            for r in active_rows:
                if r.get("status") == "claimed":
                    c_at = _parse_iso_ts(r.get("claimed_at")) or _parse_iso_ts(r.get("created_at"))
                    if c_at and c_at < stale_cutoff:
                        sb.table("telegram_inbox_buffer").update(
                            {"status": "buffered", "claim_token": None}
                        ).eq("id", r["id"]).eq("status", "claimed").execute()
                        recovered_ids.add(str(r["id"]))

            buffered_rows = [
                r
                for r in active_rows
                if r.get("status") == "buffered" or str(r.get("id")) in recovered_ids
            ]
            if not buffered_rows:
                return

            if debounce_s > 0 and attempt < 2:
                created_times = [
                    ts
                    for ts in (_parse_iso_ts(r.get("created_at")) for r in buffered_rows)
                    if ts is not None
                ]
                if created_times:
                    latest_ts = max(created_times)
                    remaining = debounce_s - (now_utc - latest_ts).total_seconds()
                    if 0.05 < remaining <= debounce_s:
                        await asyncio.sleep(remaining)
                        continue

            claim_token = str(uuid.uuid4())
            target_ids = [r["id"] for r in buffered_rows]
            claimed_rows = (
                sb.table("telegram_inbox_buffer")
                .update(
                    {
                        "status": "claimed",
                        "claim_token": claim_token,
                        "claimed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .eq("chat_id", chat_id)
                .eq("user_id", user_id)
                .eq("status", "buffered")
                .in_("id", target_ids)
                .execute()
                .data
                or []
            )
            break

    if not claimed_rows:
        return

    row_ids = [r["id"] for r in claimed_rows]
    domain_channel = claimed_rows[0].get("domain_channel") or _resolve_domain_channel(chat_id, settings)

    # Hold Attendance Group cleanly for Wave 2 rollout
    if domain_channel == "attendance":
        sb.table("telegram_inbox_buffer").update(
            {"status": "ignored", "processed_at": datetime.now(timezone.utc).isoformat()}
        ).in_("id", row_ids).execute()
        return

    try:
        merged = merge_buffered_telegram_burst(claimed_rows)
        first_raw = claimed_rows[0].get("raw_payload") or {}
        first_msg = first_raw.get("message") or first_raw.get("edited_message") or {}
        is_group = first_msg.get("chat", {}).get("type", "private") != "private"
        is_reply_to_bot = bool(first_msg.get("reply_to_message", {}).get("from", {}).get("is_bot"))
        has_media = bool(merged["photo_file_ids"] or merged["document_file_ids"])
        has_voice = bool(merged["voice_file_ids"])
        combined_text = merged["combined_text"]

        # Surgical Decisions API group-chatter filter (text-only unaddressed group messages)
        if (
            is_group
            and not is_reply_to_bot
            and not has_media
            and not has_voice
            and should_run_decisions_front_door(
                is_group_chat=True,
                is_bot_mentioned_or_reply=("@" in combined_text or combined_text.startswith("/")),
                has_photo_or_pdf=False,
                domain_channel=domain_channel,
            )
        ):
            is_rel = await asyncio.to_thread(
                decisions_triage.is_group_message_relevant, combined_text, domain_channel
            )
            if not is_rel:
                sb.table("telegram_inbox_buffer").update(
                    {"status": "ignored", "processed_at": datetime.now(timezone.utc).isoformat()}
                ).in_("id", row_ids).execute()
                return

        voice_clips: list[tuple[bytes, str]] = []
        for vfid in merged["voice_file_ids"]:
            v_data, _ = await telegram_api.download_file_bytes(vfid)
            if v_data:
                voice_clips.append((v_data, "audio/ogg"))
        audio_bytes = voice_clips[0][0] if voice_clips else None
        extra_audio_files = voice_clips[1:] or None

        media_files: list[tuple[bytes, str]] = []
        for fid in merged["photo_file_ids"]:
            b_data, _ = await telegram_api.download_file_bytes(fid)
            media_files.append((b_data, "image/jpeg"))
        for dfid in merged["document_file_ids"]:
            b_data, _ = await telegram_api.download_file_bytes(dfid)
            media_files.append((b_data, "application/pdf"))

        sender_name = (first_msg.get("from") or {}).get("first_name") or "Operator"
        actor_person_id, actor_label = _resolve_person_by_telegram(sb, user_id, sender_name)
        scope = "munim" if domain_channel == "munim" else "production"

        turn_res = await agent_core.run_agent_turn(
            sb,
            owner_person_id=actor_person_id,
            actor_label=actor_label,
            source_channel="telegram",
            domain_scope=scope,
            text=combined_text or None,
            audio_bytes=audio_bytes,
            audio_mime_type="audio/ogg",
            extra_audio_files=extra_audio_files,
            media_files=media_files or None,
            telegram_chat_id=chat_id,
        )

        heard_hdr = format_voice_receipt_header(turn_res.get("heard_transcript"))
        reply_to = merged["latest_message_id"]

        if not turn_res.get("goals"):
            await telegram_api.send_message(
                chat_id=chat_id,
                text=f"{heard_hdr}{turn_res.get('reply_text') or 'Done.'}",
                reply_to_message_id=reply_to,
            )
        else:
            for idx, g_out in enumerate(turn_res["goals"]):
                prefix = heard_hdr if idx == 0 else ""
                st = g_out.get("status")
                if st == "completed":
                    body = f"{prefix}✅ *Updated*\n{g_out.get('receipt') or g_out.get('summary_title')}"
                elif st == "gathering_info":
                    body = f"{prefix}❓ *{g_out.get('summary_title') or 'Quick Check'}*\n{g_out.get('prompt')}"
                elif st == "needs_error_fix":
                    body = f"{prefix}{g_out.get('prompt')}"
                else:
                    body = f"{prefix}{g_out.get('prompt') or g_out.get('summary_title')}"

                kb = _build_goal_keyboard(g_out)
                sent = await telegram_api.send_message(
                    chat_id=chat_id,
                    text=body,
                    reply_markup=kb,
                    reply_to_message_id=reply_to if idx == 0 else None,
                )
                bot_mid = (sent.get("result") or {}).get("message_id")
                if bot_mid and g_out.get("goal_id"):
                    sb.table("agent_goals").update({"telegram_bot_msg_id": bot_mid}).eq(
                        "id", g_out["goal_id"]
                    ).execute()

        sb.table("telegram_inbox_buffer").update(
            {"status": "processed", "processed_at": datetime.now(timezone.utc).isoformat()}
        ).in_("id", row_ids).execute()
    except Exception as exc:
        logger.exception("Error processing debounced Telegram burst for chat=%s user=%s", chat_id, user_id)
        sb.table("telegram_inbox_buffer").update(
            {"status": "failed", "processed_at": datetime.now(timezone.utc).isoformat()}
        ).in_("id", row_ids).execute()


# ---------------------------------------------------------------------------
# FastAPI Routes
# ---------------------------------------------------------------------------
@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _verify_webhook_secret(x_telegram_bot_api_secret_token, settings.telegram_webhook_secret)
    update: dict[str, Any] = await request.json()
    update_id = update.get("update_id")
    if update_id is None:
        return {"ok": True}

    user_id, chat_id = _extract_user_and_chat(update)
    domain_channel = _resolve_domain_channel(chat_id or 0, settings)

    # Strict internal operator allowlist (Attendance channel held separately for Wave 2)
    if (
        domain_channel != "attendance"
        and settings.allowed_telegram_id_set
        and user_id not in settings.allowed_telegram_id_set
    ):
        logger.warning("Ignored unauthorized Telegram user_id=%s", user_id)
        return {"ok": True, "ignored": "unauthorized_user"}

    sb = get_supabase_admin()

    # 1. Idempotent update_id dedup (covers messages AND callback_query retries)
    try:
        sb.table("telegram_webhook_dedup").insert(
            {
                "telegram_update_id": int(update_id),
                "chat_id": int(chat_id or 0),
                "user_id": int(user_id or 0) if user_id else None,
            }
        ).execute()
    except Exception as exc:
        if any(k in str(exc).lower() for k in ("23505", "duplicate", "unique")):
            return {"ok": True, "deduped": True}
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook dedup insert failed.",
        ) from exc

    if "callback_query" in update:
        return await _handle_agent_callback(sb, update["callback_query"])

    msg = update.get("message") or update.get("edited_message") or {}
    if not msg or not chat_id or not user_id:
        return {"ok": True}

    # 2. Insert into 2.5s burst buffer
    sb.table("telegram_inbox_buffer").insert(
        {
            "telegram_update_id": int(update_id),
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "domain_channel": domain_channel,
            "message_id": msg.get("message_id"),
            "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
            "media_group_id": msg.get("media_group_id"),
            "raw_payload": update,
            "status": "buffered",
        }
    ).execute()

    worker_payload = {"chat_id": int(chat_id), "user_id": int(user_id)}
    if settings.cloud_tasks_enabled:
        _dispatch_to_cloud_tasks(
            worker_payload,
            settings,
            delay_seconds=settings.telegram_burst_debounce_seconds,
        )
    else:
        background_tasks.add_task(process_debounced_burst, int(chat_id), int(user_id), True)

    return {"ok": True, "buffered": True}


@router.post("/tasks/telegram-worker")
async def telegram_worker(
    request: Request,
    x_cron_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.cron_auth_secret and not hmac.compare_digest(
        x_cron_secret or "", settings.cron_auth_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid task worker secret.",
        )
    payload: dict[str, Any] = await request.json()
    chat_id = int(payload.get("chat_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if chat_id and user_id:
        await process_debounced_burst(chat_id, user_id, wait_locally=False)
    return {"ok": True}


@router.post("/tasks/goal-retry")
async def goal_retry_worker(
    request: Request,
    x_cron_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.cron_auth_secret and not hmac.compare_digest(
        x_cron_secret or "", settings.cron_auth_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid task worker secret.",
        )
    payload: dict[str, Any] = await request.json()
    goal_id = str(payload.get("goal_id") or "")
    if not goal_id:
        return {"ok": False, "error": "Missing goal_id"}
    return await goal_intelligence.execute_scheduled_goal_retry(
        get_supabase_admin(),
        goal_id=goal_id,
        actor_person_id=payload.get("actor_person_id"),
        actor_label=str(payload.get("actor_label") or "Auto-Retry"),
    )
