"""Captionless attachment hold & 15-minute unlinked desk memory for AgentCore (Rec #2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Any

from app.services import goal_intelligence, storage

logger = logging.getLogger(__name__)

CAPTIONLESS_HOLD_PROMPT = (
    "📎 File mil gayi — kripya batayein yeh kis customer/order ya supplier bill ke liye hai "
    "(e.g. 'New order Arjun Jaipur' ya 'Add items to Vikas order')."
)

ATTACHMENT_REF_RE = re.compile(
    r"(?:\b(?:above\s+(?:pdf|photo|image|file|pic|sheet|doc)|"
    r"this\s+(?:pdf|photo|image|file|bill|sheet)|attached|attachment|"
    r"upar\s+(?:wala|wali|ka)?|pichl[ae]\s+(?:pdf|photo|bill|file)|"
    r"yeh?\s+(?:pdf|photo|bill|file))\b)",
    re.IGNORECASE,
)


def _parse_iso_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def should_hold_captionless_upload(
    *,
    text: str | None,
    has_audio: bool,
    media_kinds: list[str],
    domain_scope: str,
) -> bool:
    """Hold captionless PDFs/photos politely unless it's clearly a Munim supplier bill."""
    if (text or "").strip() or has_audio or not media_kinds:
        return False
    if domain_scope == "munim" and "supplier_bill" in media_kinds:
        return False
    return True


def save_captionless_attachment_hold(
    sb: Any,
    *,
    owner_person_id: str | None,
    source_channel: str,
    domain_scope: str,
    telegram_chat_id: int | None,
    ui_context: dict[str, Any] | None,
    uploaded_gcs_paths: list[str],
    uploaded_mimes: list[str],
) -> dict[str, Any]:
    """Create or update a hold goal so a bare PDF/photo waits cleanly for follow-up text."""
    now_iso = datetime.now(timezone.utc).isoformat()
    safe_owner = goal_intelligence._safe_uuid_for_db(sb, owner_person_id)

    open_rows = (
        sb.table("agent_goals")
        .select("id,telegram_chat_id,telegram_bot_msg_id,owner_person_id,collected_slots,updated_at,created_at")
        .eq("status", "gathering_info")
        .order("updated_at", desc=True)
        .limit(15)
        .execute()
        .data
        or []
    )
    existing_hold: dict[str, Any] | None = None
    for r in open_rows:
        if not (r.get("collected_slots") or {}).get("is_attachment_hold"):
            continue
        if telegram_chat_id is not None and r.get("telegram_chat_id") == telegram_chat_id:
            existing_hold = r
            break
        if telegram_chat_id is None and safe_owner and r.get("owner_person_id") == safe_owner:
            existing_hold = r
            break

    prior_slots = dict(existing_hold.get("collected_slots") or {}) if existing_hold else {}
    merged_paths = list(dict.fromkeys([*(prior_slots.get("uploaded_gcs_paths") or []), *uploaded_gcs_paths]))
    merged_mimes = [*(prior_slots.get("uploaded_mimes") or []), *uploaded_mimes][: len(merged_paths)]
    slots = {
        "is_attachment_hold": True,
        "uploaded_gcs_paths": merged_paths,
        "uploaded_mimes": merged_mimes,
    }
    payload = {
        "owner_person_id": safe_owner,
        "source_channel": source_channel,
        "domain_scope": domain_scope,
        "telegram_chat_id": telegram_chat_id,
        "ui_context": ui_context or {},
        "goal_type": "stage_update",
        "status": "gathering_info",
        "thinking_level_used": "low",
        "summary_title": "📎 Uploaded Attachment (Awaiting Instructions)",
        "collected_slots": slots,
        "missing_slots": ["rough_scope"],
        "pending_prompt": CAPTIONLESS_HOLD_PROMPT,
        "steps": [],
        "repair_options": [],
        "updated_at": now_iso,
    }
    prior_mid = existing_hold.get("telegram_bot_msg_id") if existing_hold else None
    if existing_hold:
        res = sb.table("agent_goals").update(payload).eq("id", existing_hold["id"]).execute().data
        row = res[0]
    else:
        payload["created_at"] = now_iso
        res = sb.table("agent_goals").insert(payload).execute().data
        row = res[0]

    goal_out: dict[str, Any] = {
        "goal_id": row["id"],
        "status": "gathering_info",
        "summary_title": row["summary_title"],
        "prompt": CAPTIONLESS_HOLD_PROMPT,
        "choices": [],
        "missing_slots": ["rough_scope"],
        "collected_slots": slots,
    }
    if prior_mid:
        goal_out["superseded_bot_msg_id"] = prior_mid
    return {
        "kind": "goal_actions",
        "heard_transcript": None,
        "reply_text": None,
        "goals": [goal_out],
    }


def resolve_recent_unclaimed_attachments(
    sb: Any,
    *,
    text: str | None,
    owner_person_id: str | None,
    telegram_chat_id: int | None,
    domain_scope: str,
    window_minutes: int = 15,
) -> tuple[list[tuple[bytes, str]], list[str], list[int]]:
    """Auto-attach a captionless hold OR 15-minute recent attachment referenced in follow-up text."""
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=window_minutes)
    safe_owner = goal_intelligence._safe_uuid_for_db(sb, owner_person_id)
    has_ref_cue = bool(text and ATTACHMENT_REF_RE.search(text))

    rows = (
        sb.table("agent_goals")
        .select("id,owner_person_id,telegram_chat_id,telegram_bot_msg_id,domain_scope,status,collected_slots,updated_at,created_at")
        .order("created_at", desc=True)
        .limit(30)
        .execute()
        .data
        or []
    )

    hold_row: dict[str, Any] | None = None
    ref_row: dict[str, Any] | None = None
    for r in rows:
        if telegram_chat_id is not None:
            if r.get("telegram_chat_id") != telegram_chat_id:
                continue
        elif safe_owner and r.get("owner_person_id") != safe_owner:
            continue
        elif r.get("domain_scope") not in (None, domain_scope, "shared"):
            continue

        slots = r.get("collected_slots") or {}
        paths = [str(p) for p in (slots.get("uploaded_gcs_paths") or []) if str(p).strip()]
        if not paths:
            continue
        ts = _parse_iso_dt(r.get("updated_at")) or _parse_iso_dt(r.get("created_at"))
        if ts and ts < cutoff:
            continue

        if r.get("status") == "gathering_info" and slots.get("is_attachment_hold") and hold_row is None:
            hold_row = r
        elif has_ref_cue and r.get("status") != "discarded" and ref_row is None:
            ref_row = r

    chosen = hold_row or ref_row
    if not chosen:
        return [], [], []

    c_slots = chosen.get("collected_slots") or {}
    paths = [str(p) for p in (c_slots.get("uploaded_gcs_paths") or []) if str(p).strip()]
    mimes = [str(m) for m in (c_slots.get("uploaded_mimes") or [])]

    restored_files: list[tuple[bytes, str]] = []
    restored_paths: list[str] = []
    for idx, p in enumerate(paths):
        mime = mimes[idx] if idx < len(mimes) and mimes[idx] else (
            "application/pdf" if p.lower().endswith(".pdf") else "image/webp"
        )
        try:
            raw = storage.download_bytes(p)
            restored_files.append((raw, mime))
            restored_paths.append(p)
        except Exception as exc:
            logger.warning("Could not restore cached attachment '%s': %s", p, exc)

    retired_ids: list[int] = []
    if chosen is hold_row and restored_files:
        sb.table("agent_goals").update(
            {"status": "discarded", "resolved_at": now_utc.isoformat()}
        ).eq("id", chosen["id"]).execute()
        if chosen.get("telegram_bot_msg_id"):
            retired_ids.append(int(chosen["telegram_bot_msg_id"]))

    return restored_files, restored_paths, retired_ids
