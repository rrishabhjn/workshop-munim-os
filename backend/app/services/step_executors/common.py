"""Shared parsers, stage synonyms, and operator handoff helpers for step executors."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any

from app.services import telegram_api
from app.services.goal_primitives import (
    IST,
    ITEM_EXECUTION_STAGES,
    MATERIAL_TRACK_STAGES,
    PROJECT_PIPELINE_STAGES,
)

logger = logging.getLogger(__name__)

_DATE_NULL_STRINGS = {"", "null", "none", "nil", "tbd", "na", "n/a", "unknown", "later", "soon"}

_PIPELINE_STAGE_SYNONYMS: dict[str, str] = {
    "lead": "lead",
    "new_lead": "lead",
    "enquiry": "lead",
    "design": "design",
    "3d": "design",
    "designing": "design",
    "estimate": "estimating",
    "estimating": "estimating",
    "costing": "estimating",
    "quoted": "quoted_awaiting_advance",
    "quoted_to_customer": "quoted_awaiting_advance",
    "awaiting_advance": "quoted_awaiting_advance",
    "quote_sent": "quoted_awaiting_advance",
    "quoted_to_customer_awaiting_advance": "quoted_awaiting_advance",
    "sizing": "site_sizing_boq",
    "site_sizing": "site_sizing_boq",
    "boq": "site_sizing_boq",
    "measurement": "site_sizing_boq",
    "execution": "execution",
    "in_execution": "execution",
    "production": "execution",
    "in_production": "execution",
    "started": "execution",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "delivered": "completed",
    "hold": "on_hold",
    "on_hold": "on_hold",
    "lost": "lost",
    "dropped": "lost",
    "cancelled": "lost",
    "cancel": "lost",
}

_MATERIAL_TRACK_SYNONYMS: dict[str, str] = {
    "not_started": "not_started",
    "check_stock": "check_stock",
    "to_order": "check_stock",
    "needed": "check_stock",
    "pending": "check_stock",
    "ordered": "ordered_pending",
    "ordered_pending": "ordered_pending",
    "all_in_shop": "all_in_shop",
    "in_shop": "all_in_shop",
    "in_ship": "all_in_shop",
    "received": "all_in_shop",
}

_ITEM_STAGE_SYNONYMS: dict[str, str] = {
    "in_shop": "in_shop",
    "in_ship": "in_shop",
    "shop": "in_shop",
    "production": "in_shop",
    "making": "in_shop",
    "cutting": "in_shop",
    "packed": "packed_ready",
    "ready": "packed_ready",
    "packed_ready": "packed_ready",
    "packed_and_ready": "packed_ready",
    "at_site": "at_site",
    "site": "at_site",
    "dispatched": "at_site",
    "fitting": "at_site",
    "assembly": "at_site",
    "qc": "qc_done",
    "qc_done": "qc_done",
    "checked": "qc_done",
}


def _parse_iso_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def _compute_duration_hours(entered_at_raw: str | None) -> float | None:
    dt = _parse_iso_dt(entered_at_raw)
    if not dt:
        return None
    secs = max((datetime.now(timezone.utc) - dt).total_seconds(), 0.0)
    return round(secs / 3600.0, 2)


def sanitize_iso_date(raw: Any) -> str | None:
    """Parse common ISO / Indian DD-MM-YYYY / today / tomorrow date strings into YYYY-MM-DD or None."""
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    txt = str(raw).strip()
    if not txt or txt.lower() in _DATE_NULL_STRINGS:
        return None
    low = txt.lower()
    today_ist = datetime.now(IST).date()
    if low in ("today", "aaj"):
        return today_ist.isoformat()
    if low in ("tomorrow", "kal"):
        return (today_ist + timedelta(days=1)).isoformat()

    m_iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s].*)?$", txt)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))).isoformat()
        except ValueError:
            return None

    m_dmy = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", txt)
    if m_dmy:
        try:
            return date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1))).isoformat()
        except ValueError:
            return None

    return None


def parse_positive_quantity(raw: Any, default: float = 1.0) -> tuple[float, str | None]:
    """Parse quantity from int/float or strings like '8 sheets' -> (8.0, 'sheets')."""
    if raw is None:
        return default, None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = round(float(raw), 2)
        return (max(val, 0.01), None) if val > 0 else (default, None)
    txt = str(raw).strip().replace(",", "")
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s*$", txt)
    if m:
        val = round(float(m.group(1)), 2)
        unit = m.group(2).lower() if m.group(2) else None
        return (max(val, 0.01), unit) if val > 0 else (default, unit)
    return default, None


def normalize_stage_code_for_op(op: str, raw_stage: str | None) -> str | None:
    """Normalize human/Hinglish stage labels into canonical DB stage codes."""
    if not raw_stage or not str(raw_stage).strip():
        return None
    raw_clean = str(raw_stage).strip()
    key = re.sub(r"[\s/_-]+", "_", raw_clean.lower()).strip("_")
    if op in ("pipeline_stage_move", "new_lead"):
        if raw_clean in PROJECT_PIPELINE_STAGES:
            return raw_clean
        return _PIPELINE_STAGE_SYNONYMS.get(key, raw_clean)
    if op == "material_stage_move":
        if raw_clean in MATERIAL_TRACK_STAGES:
            return raw_clean
        return _MATERIAL_TRACK_SYNONYMS.get(key, raw_clean)
    if op == "item_upsert_or_move":
        if raw_clean in ITEM_EXECUTION_STAGES:
            return raw_clean
        return _ITEM_STAGE_SYNONYMS.get(key, raw_clean)
    return raw_clean


def get_default_person_id(sb: Any, flag_col: str) -> str | None:
    """Lookup default purchase (Ansh) or default QC (Shreyansh) person UUID."""
    rows = (
        sb.table("people")
        .select("id")
        .eq("is_active", True)
        .eq(flag_col, True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return str(rows[0]["id"]) if rows else None


async def _notify_operator_handoff(
    sb: Any,
    assignee_ids: list[str],
    actor_person_id: str | None,
    headline: str,
) -> None:
    """Anti-spam handoff rule: ping ONLY office operators who have a telegram_user_id
    (and skip pinging the person who just performed the action themselves).
    """
    targets = [pid for pid in assignee_ids if pid and pid != actor_person_id]
    if not targets:
        return
    rows = (
        sb.table("people")
        .select("id,full_name,person_type,telegram_user_id")
        .in_("id", targets)
        .eq("person_type", "operator")
        .not_.is_("telegram_user_id", "null")
        .execute()
        .data
        or []
    )
    for r in rows:
        tg_id = r.get("telegram_user_id")
        if tg_id:
            try:
                await telegram_api.send_message(
                    chat_id=int(tg_id),
                    text=f"🔔 *Action Assigned to You*\n{headline}",
                )
            except Exception as exc:
                logger.warning("Operator handoff ping skipped for %s: %s", r.get("full_name"), exc)


def _format_customer_order_display_name(full_name: str, area_landmark: str | None) -> str:
    cname = full_name.strip()
    area = (area_landmark or "").strip()
    if area and area.lower() not in cname.lower():
        return f"{cname} ({area})"
    return cname


def _norm_phone10(raw_p: Any) -> str:
    digits = re.sub(r"\D+", "", str(raw_p or ""))
    return digits[-10:] if len(digits) >= 10 else digits
