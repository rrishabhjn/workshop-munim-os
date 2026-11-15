from datetime import date, timedelta
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.db import get_supabase_admin
from app.services import sheets_backup, telegram_api, zoho

router = APIRouter(prefix="/cron", tags=["cron"])


def _verify_cron_secret(header_val: str | None, expected: str) -> None:
    if expected and not hmac.compare_digest(header_val or "", expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid cron auth secret.",
        )


@router.post("/morning-brief")
async def morning_brief(
    x_cron_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """08:30 IST Daily Morning Brief split into Floor Brief (price-free) & Accounts Brief."""
    _verify_cron_secret(x_cron_secret, settings.cron_auth_secret)
    sb = get_supabase_admin()

    today = date.today()
    three_days = (today + timedelta(days=3)).isoformat()

    upcoming_projects = (
        sb.table("projects")
        .select("project_code,customer_name,promised_delivery_date")
        .eq("status", "active")
        .eq("project_type", "customer")
        .lte("promised_delivery_date", three_days)
        .order("promised_delivery_date")
        .execute()
        .data
        or []
    )
    mat_pending = (
        sb.table("project_items")
        .select("id")
        .eq("stage", "material_pending")
        .execute()
        .data
        or []
    )
    purchase_items = (
        sb.table("v_master_purchase_list")
        .select("material_name")
        .execute()
        .data
        or []
    )
    open_followups = (
        sb.table("followups")
        .select("name,due_date,action_note")
        .eq("status", "open")
        .lte("due_date", today.isoformat())
        .limit(5)
        .execute()
        .data
        or []
    )
    unverified_cash = (
        sb.table("cash_ledger")
        .select("id")
        .eq("verified_by_staff", False)
        .execute()
        .data
        or []
    )
    pnl_rows = (
        sb.table("v_project_pnl")
        .select("customer_name,balance_pending")
        .eq("status", "active")
        .eq("project_type", "customer")
        .gt("balance_pending", 0)
        .order("balance_pending", desc=True)
        .limit(4)
        .execute()
        .data
        or []
    )

    due_summary = (
        ", ".join(
            f"{p['customer_name']} ({p['promised_delivery_date']})"
            for p in upcoming_projects[:4]
        )
        or "None due in next 3 days"
    )
    followup_summary = (
        ", ".join(f"{f['name']}: {f['action_note']}" for f in open_followups[:3])
        or "No overdue follow-ups"
    )
    receivables_summary = (
        ", ".join(
            f"{r['customer_name']} (₹{float(r.get('balance_pending') or 0):,.0f})"
            for r in pnl_rows
        )
        or "All caught up"
    )

    floor_brief = (
        f"☀️ *Workshop Floor — Morning Brief ({today.strftime('%d %b')})*\n\n"
        f"• *Deliveries Due (≤3d):* {due_summary}\n"
        f"• *Material Blocked:* {len(mat_pending)} piece(s) waiting | {len(purchase_items)} line(s) on Purchase List\n"
        f"• *Follow-Ups Due:* {followup_summary}"
    )
    accounts_brief = (
        f"📒 *Munim & Accounts — Morning Brief ({today.strftime('%d %b')})*\n\n"
        f"• *Unverified Cash Entries:* {len(unverified_cash)} awaiting staff check\n"
        f"• *Top Pending Receivables:* {receivables_summary}\n"
        f"• *Deliveries Due (≤3d):* {due_summary}"
    )

    kb = {
        "inline_keyboard": [[
            {"text": "📊 Open Dashboard", "url": settings.app_base_url}
        ]]
    }

    sent_targets: list[str] = []
    if settings.telegram_bot_token:
        floor_target = settings.telegram_floor_chat_id.strip()
        accounts_target = settings.telegram_accounts_chat_id.strip()

        if floor_target:
            await telegram_api.send_message(
                chat_id=floor_target, text=floor_brief, reply_markup=kb
            )
            sent_targets.append(f"floor:{floor_target}")

        if accounts_target:
            await telegram_api.send_message(
                chat_id=accounts_target, text=accounts_brief, reply_markup=kb
            )
            sent_targets.append(f"accounts:{accounts_target}")

        if not floor_target and not accounts_target:
            fallback = settings.telegram_morning_brief_chat_id.strip() or (
                str(next(iter(settings.allowed_telegram_id_set)))
                if settings.allowed_telegram_id_set
                else ""
            )
            if fallback:
                await telegram_api.send_message(
                    chat_id=fallback,
                    text=f"{floor_brief}\n• *Staff Review:* {len(unverified_cash)} unverified cash entry/entries",
                    reply_markup=kb,
                )
                sent_targets.append(f"fallback:{fallback}")

    return {"ok": True, "sent_targets": sent_targets}


@router.post("/nightly-sync")
async def nightly_sync(
    x_cron_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """02:00 IST Zoho financial cache refresh + one-way read-only Google Sheet backup."""
    _verify_cron_secret(x_cron_secret, settings.cron_auth_secret)
    sb = get_supabase_admin()
    zoho_res = await zoho.sync_project_financial_caches(sb)
    sheet_res = sheets_backup.export_readonly_sheet_snapshot(sb)
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    sb.table("telegram_state").delete().lt("created_at", f"{cutoff}T00:00:00Z").execute()
    return {"ok": True, "zoho": zoho_res, "sheets": sheet_res}
