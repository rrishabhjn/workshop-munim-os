from datetime import datetime, timedelta
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.db import get_supabase_admin
from app.services import sheets_backup, step_executors, telegram_api, zoho
from app.services.goal_primitives import IST, format_inr

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
    """08:30 IST Anti-Spam Daily Brief for Production + Munim (including Needs Info footer)."""
    _verify_cron_secret(x_cron_secret, settings.cron_auth_secret)
    sb = get_supabase_admin()

    today = datetime.now(IST).date()
    three_days = (today + timedelta(days=3)).isoformat()

    upcoming_projects = (
        sb.table("projects")
        .select("project_code,customer_name,promised_delivery_date,pipeline_stage")
        .eq("status", "active")
        .eq("project_type", "customer")
        .lte("promised_delivery_date", three_days)
        .order("promised_delivery_date")
        .execute()
        .data
        or []
    )
    wdw = step_executors.enrich_whos_doing_what_rows(
        sb, sb.table("v_whos_doing_what").select("*").execute().data or []
    )
    stale_items = [w for w in wdw if int(w.get("days_in_stage") or 0) >= 3]
    ansh_pending = [w for w in wdw if w.get("work_kind") == "material_track"]
    packed_gate = [w for w in wdw if w.get("stage_code") == "packed_ready"]

    inv_items = (
        sb.table("inventory_items")
        .select("name,on_hand_qty,min_stock_qty")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    low_stock_names = [
        it["name"]
        for it in inv_items
        if float(it.get("min_stock_qty") or 0) > 0
        and float(it.get("on_hand_qty") or 0) <= float(it.get("min_stock_qty") or 0)
    ]
    rate_pending_rows = (
        sb.table("inventory_movements")
        .select("id,is_undone")
        .eq("zoho_sync_status", "rate_pending")
        .execute()
        .data
        or []
    )
    rate_pending_cnt = sum(1 for m in rate_pending_rows if not m.get("is_undone"))
    inv_line = (
        f"\n• *Inventory Radar:* {len(low_stock_names)} low stock, {rate_pending_cnt} rate pending"
        if (low_stock_names or rate_pending_cnt)
        else ""
    )

    needs_info = sb.table("v_needs_info_queue").select("goal_id,summary_title,status,domain_scope").limit(10).execute().data or []
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
        ", ".join(f"{p['customer_name']} ({p['promised_delivery_date']})" for p in upcoming_projects[:4])
        or "None due ≤3d"
    )
    stale_summary = (
        ", ".join(f"{s['customer_name']} [{s['stage_label']} · {s['days_in_stage']}d]" for s in stale_items[:4])
        or "Nothing stuck ≥3d"
    )
    receivables_summary = (
        ", ".join(f"{r['customer_name']} ({format_inr(float(r.get('balance_pending') or 0))})" for r in pnl_rows)
        or "All caught up"
    )
    prod_needs = [n for n in needs_info if n.get("domain_scope") != "munim"]
    munim_needs = [n for n in needs_info if n.get("domain_scope") in ("munim", "shared")]
    prod_footer = (
        f"\n\n📌 *Needs Info / Clerk Queue ({len(prod_needs)}):* "
        + "; ".join(n.get("summary_title") or n["status"] for n in prod_needs[:3])
        if prod_needs
        else ""
    )
    munim_footer = (
        f"\n\n📌 *Needs Info / Clerk Queue ({len(munim_needs)}):* "
        + "; ".join(n.get("summary_title") or n["status"] for n in munim_needs[:3])
        if munim_needs
        else ""
    )

    prod_brief = (
        f"☀️ *Production Morning Brief ({today.strftime('%d %b')})*\n\n"
        f"• *Deliveries Due (≤3d):* {due_summary}\n"
        f"• *Stuck ≥3 Days:* {stale_summary}\n"
        f"• *Ansh Material Queue:* {len(ansh_pending)} order(s) | *Packed / Ready Gate:* {len(packed_gate)} piece(s)"
        f"{inv_line}"
        f"{prod_footer}"
    )
    munim_brief = (
        f"📒 *Munim Morning Brief ({today.strftime('%d %b')})*\n\n"
        f"• *Top Pending Receivables:* {receivables_summary}\n"
        f"• *Deliveries Due (≤3d):* {due_summary}"
        f"{munim_footer}"
    )

    prod_kb = {"inline_keyboard": [[{"text": "🏭 Open Production", "url": f"{settings.app_base_url.rstrip('/')}/production"}]]}
    munim_kb = {"inline_keyboard": [[{"text": "📒 Open Munim", "url": f"{settings.app_base_url.rstrip('/')}/munim"}]]}

    sent_targets: list[str] = []
    if settings.telegram_bot_token:
        prod_target = settings.effective_production_chat_id
        munim_target = settings.effective_munim_chat_id

        if prod_target:
            await telegram_api.send_message(chat_id=prod_target, text=prod_brief, reply_markup=prod_kb)
            sent_targets.append(f"production:{prod_target}")

        if munim_target:
            await telegram_api.send_message(chat_id=munim_target, text=munim_brief, reply_markup=munim_kb)
            sent_targets.append(f"munim:{munim_target}")

        if not prod_target and not munim_target:
            fallback = settings.telegram_morning_brief_chat_id.strip() or (
                str(next(iter(settings.allowed_telegram_id_set))) if settings.allowed_telegram_id_set else ""
            )
            if fallback:
                await telegram_api.send_message(
                    chat_id=fallback,
                    text=f"{prod_brief}\n• *Receivables:* {receivables_summary}",
                    reply_markup=prod_kb,
                )
                sent_targets.append(f"fallback:{fallback}")

    return {"ok": True, "sent_targets": sent_targets}


@router.post("/nightly-sync")
async def nightly_sync(
    x_cron_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """02:00 IST Zoho Owner Pulse balance sync + project P&L cache sync + read-only Google Sheet backup."""
    _verify_cron_secret(x_cron_secret, settings.cron_auth_secret)
    sb = get_supabase_admin()
    pulse_res = await zoho.sync_owner_pulse_snapshot(sb)
    prj_sync_res = await zoho.sync_project_financial_caches(sb)
    sheet_res = sheets_backup.export_readonly_sheet_snapshot(sb)
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    sb.table("telegram_webhook_dedup").delete().lt("received_at", f"{cutoff}T00:00:00Z").execute()
    sb.table("telegram_inbox_buffer").delete().in_("status", ["processed", "ignored", "failed"]).lt("created_at", f"{cutoff}T00:00:00Z").execute()
    return {
        "ok": True,
        "zoho_pulse": pulse_res,
        "project_caches": prj_sync_res,
        "sheets": sheet_res,
    }
