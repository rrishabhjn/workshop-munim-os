import asyncio
from datetime import datetime, timezone
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from google.cloud import tasks_v2

from app.config import Settings, get_settings
from app.db import get_supabase_admin
from app.services import gemini_router, storage, telegram_api, zoho

router = APIRouter(tags=["telegram"])
logger = logging.getLogger(__name__)

_QUERY_HINT_WORDS = (
    "?", "kitna", "kitni", "kahan", "kaun", "kab", "baki", "status",
    "bill", "stock", "ply", "sunmica", "karigar", "order", "delivery",
    "cash", "udhaar", "payment", "ready", "polish", "carpentry",
    "paid", "pay", "diya", "diye", "received", "mila", "mile", "liya",
    "kharch", "expense", "transport", "bhada", "salary", "advance",
    "mistri", "rs", "₹", "new", "add", "create", "update", "followup",
    "supplier", "vendor", "customer", "labor", "labour", "paint",
    "painter", "wrong", "galat", "nahi", "change", "make",
)


def _verify_webhook_secret(
    secret_header: str | None, expected: str
) -> None:
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


def _resolve_group_scope(chat_id: int | str, settings: Settings) -> str:
    cid = str(chat_id)
    if settings.telegram_floor_chat_id and cid == settings.telegram_floor_chat_id.strip():
        return "floor"
    if settings.telegram_accounts_chat_id and cid == settings.telegram_accounts_chat_id.strip():
        return "accounts"
    return "dm"


def _should_process_group_text(msg: dict[str, Any]) -> bool:
    """In group chats, avoid interrupting casual human chatter unless addressed or workshop-relevant."""
    chat_type = msg.get("chat", {}).get("type", "private")
    if chat_type == "private":
        return True
    if msg.get("voice") or msg.get("audio") or msg.get("photo") or msg.get("document"):
        return True
    if msg.get("reply_to_message", {}).get("from", {}).get("is_bot"):
        return True
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return False
    if text.startswith("/") or "@" in text:
        return True
    if any(ch.isdigit() for ch in text):
        return True
    return any(kw in text for kw in _QUERY_HINT_WORDS)


def _dispatch_to_cloud_tasks(
    update: dict[str, Any], settings: Settings
) -> None:
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.gcp_project_id, settings.gcp_region, settings.cloud_tasks_queue
    )
    target_url = f"{settings.app_base_url.rstrip('/')}/tasks/telegram-worker"
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": target_url,
            "headers": {
                "Content-Type": "application/json",
                "X-Cron-Secret": settings.cron_auth_secret,
            },
            "body": json.dumps(update).encode("utf-8"),
        }
    }
    client.create_task(request={"parent": parent, "task": task})


def _normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    val = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            pass
    cur_year = datetime.now(timezone.utc).year
    for fmt in ("%B %d", "%b %d", "%d %B", "%d %b", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            dt = datetime.strptime(val, fmt).date()
            if "%Y" not in fmt:
                dt = dt.replace(year=cur_year)
            return dt.isoformat()
        except ValueError:
            pass
    return None


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


def _normalize_followup_entity_type(raw: str | None) -> str:
    val = (raw or "customer").strip().lower()
    if val in ("customer", "supplier", "karigar", "internal"):
        return val
    if val == "vendor":
        return "supplier"
    if val in ("employee", "mistri", "worker"):
        return "karigar"
    return "internal"


def _resolve_lookup_code(
    sb: Any, table: str, raw_code: str | None, default_code: str
) -> str:
    """Validate a foreign-key lookup code (workshop_stages / material_categories) with safe fallback."""
    code = (raw_code or "").strip().lower()
    if not code:
        return default_code
    rows = sb.table(table).select("code").execute().data or []
    valid = {r["code"] for r in rows}
    if code in valid:
        return code
    return default_code if default_code in valid else (next(iter(valid), default_code))


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


async def _commit_pending_actions(
    sb: Any, pending: dict[str, Any], actor_label: str
) -> tuple[list[str], dict[str, Any] | None]:
    """Commit confirmed voice/photo actions to PostgreSQL + Zoho, or return ambiguous contact prompt."""
    receipts: list[str] = []
    actions = pending.get("actions", [])
    photo_paths = pending.get("uploaded_photo_paths", [])
    newly_created_project_id: str | None = None
    contact_notes: dict[int, str] = {}

    # Pre-resolve Zoho contacts for new customers/vendors before mutating Postgres
    for idx, act in enumerate(actions):
        atype = act.get("action_type")
        party: str | None = None
        ctype: str | None = None
        phone: str | None = None

        if atype == "create_project" and (act.get("customer_name") or act.get("project_label")):
            party = (act.get("customer_name") or act.get("project_label")).strip()
            ctype = "customer"
            phone = act.get("phone")
        elif atype == "log_cash" and act.get("amount_inr") is not None:
            direction = "in" if act.get("direction") == "in" else "out"
            ttype = _normalize_txn_type(act.get("txn_type"), direction)
            if ttype in ("customer_receipt", "cash_advance", "cash_balance"):
                party = (act.get("party_or_karigar") or "").strip()
                ctype = "customer"
            elif ttype == "vendor_payment":
                party = (act.get("party_or_karigar") or "").strip()
                ctype = "vendor"

        if party and ctype and party.lower() != "unknown":
            res = await zoho.resolve_or_create_contact(
                sb,
                party,
                ctype,
                phone=phone,
                force_create=bool(act.get("force_create_contact")),
                selected_contact_id=act.get("zoho_contact_id"),
                selected_contact_name=act.get("zoho_contact_name"),
            )
            if res.get("status") == "ambiguous":
                return [], {
                    "action_index": idx,
                    "party_name": party,
                    "contact_type": ctype,
                    "matches": res["matches"],
                }
            if res.get("contact_id"):
                act["zoho_contact_id"] = res["contact_id"]
                act["zoho_contact_name"] = res.get("contact_name") or party
                if res.get("status") == "created":
                    contact_notes[idx] = f" + New Zoho {ctype.title()} created"

    for idx, act in enumerate(actions):
        atype = act.get("action_type")

        if atype == "create_project" and (act.get("customer_name") or act.get("project_label")):
            cname = act.get("zoho_contact_name") or (act.get("customer_name") or act.get("project_label")).strip()
            code = f"PRJ-{datetime.now(timezone.utc):%y%m%d-%H%M}"
            res = sb.table("projects").insert(
                {
                    "project_code": code,
                    "customer_name": cname,
                    "phone": act.get("phone"),
                    "promised_delivery_date": _normalize_date(act.get("promised_delivery_date")),
                    "quoted_total": round(float(act.get("quoted_total") or 0), 2),
                }
            ).execute()
            if res.data:
                newly_created_project_id = res.data[0]["id"]
            receipts.append(f"New Order: {cname} ({code}){contact_notes.get(idx, '')}")

        elif atype == "create_item" and (act.get("item_label") or act.get("summary")):
            pid = act.get("project_id") or newly_created_project_id
            if pid:
                iname = act.get("item_label") or act.get("summary")
                stage_code = _resolve_lookup_code(
                    sb, "workshop_stages", act.get("stage_code"), "design_measurement"
                )
                sb.table("project_items").insert(
                    {
                        "project_id": pid,
                        "item_name": iname,
                        "dimensions": act.get("dimensions"),
                        "assigned_karigar": act.get("assigned_karigar"),
                        "stage": stage_code,
                    }
                ).execute()
                receipts.append(f"New Piece: {iname}")

        elif atype == "update_stage" and act.get("item_id") and act.get("stage_code"):
            stage_code = _resolve_lookup_code(
                sb, "workshop_stages", act.get("stage_code"), "design_measurement"
            )
            sb.table("project_items").update(
                {"stage": stage_code}
            ).eq("id", act["item_id"]).execute()
            receipts.append(f"Stage updated: {act.get('summary', act['item_id'])}")

        elif atype == "add_material" and (act.get("project_id") or newly_created_project_id) and act.get("item_id"):
            cat_code = _resolve_lookup_code(
                sb, "material_categories", act.get("material_category"), "ply_wood"
            )
            qty = max(float(act.get("quantity") or 1), 0.01)
            sb.table("item_materials").insert(
                {
                    "project_id": act.get("project_id") or newly_created_project_id,
                    "item_id": act["item_id"],
                    "category": cat_code,
                    "material_name": act.get("material_name") or "Material",
                    "quantity": qty,
                    "unit": act.get("unit") or "pcs",
                    "status": "needed",
                    "preferred_supplier": act.get("preferred_supplier"),
                }
            ).execute()
            receipts.append(f"Material added: {act.get('summary', act.get('material_name'))}")

        elif atype == "log_cash" and act.get("amount_inr") is not None:
            party = act.get("party_or_karigar") or "Unknown"
            direction = "in" if act.get("direction") == "in" else "out"
            ttype = _normalize_txn_type(act.get("txn_type"), direction)
            amt = max(round(float(act["amount_inr"]), 2), 0.0)
            pmode = act.get("payment_mode") or "cash"
            txn_date = _normalize_date(act.get("due_date"))
            full_note = (act.get("notes") or act.get("action_note") or act.get("summary") or "").strip()
            ins = sb.table("cash_ledger").insert(
                {
                    "txn_type": ttype,
                    "direction": direction,
                    "party_or_karigar": party,
                    "project_id": act.get("project_id") or newly_created_project_id,
                    "item_id": act.get("item_id"),
                    "amount_inr": amt,
                    "payment_mode": pmode,
                    "notes": full_note or act.get("summary"),
                    "logged_by": actor_label,
                }
            ).execute()
            z_res = await zoho.sync_cash_entry_to_zoho(
                txn_type=ttype,
                direction=direction,
                party_or_karigar=act.get("zoho_contact_name") or party,
                amount_inr=amt,
                txn_date=txn_date,
                logged_by=actor_label,
                notes=full_note,
                payment_mode=pmode,
                contact_id=act.get("zoho_contact_id"),
            )
            if ins.data:
                sb.table("cash_ledger").update({"metadata": {"zoho_sync": z_res}}).eq("id", ins.data[0]["id"]).execute()
            if z_res.get("synced"):
                z_note = f" → Zoho ({z_res.get('zoho_type')}){contact_notes.get(idx, '')}"
            elif z_res.get("error"):
                z_note = f" (Supabase only: {str(z_res['error'])[:80]})"
            else:
                z_note = ""
            receipts.append(f"Cash logged (by {actor_label}): {act.get('summary', amt)}{z_note}")

        elif atype == "add_followup" and act.get("action_note"):
            sb.table("followups").insert(
                {
                    "due_date": _normalize_date(act.get("due_date")) or datetime.now(timezone.utc).date().isoformat(),
                    "entity_type": _normalize_followup_entity_type(act.get("entity_type")),
                    "name": act.get("followup_name") or act.get("party_or_karigar") or "Follow-up",
                    "project_id": act.get("project_id") or newly_created_project_id,
                    "action_note": act["action_note"],
                }
            ).execute()
            receipts.append(f"Follow-up added: {act.get('summary', act['action_note'])}")

        elif atype == "attach_photos" and act.get("item_id") and photo_paths:
            existing = (
                sb.table("project_items")
                .select("photo_paths")
                .eq("id", act["item_id"])
                .limit(1)
                .execute()
                .data
            )
            current_paths = (existing[0].get("photo_paths") or []) if existing else []
            sb.table("project_items").update(
                {"photo_paths": current_paths + photo_paths}
            ).eq("id", act["item_id"]).execute()
            receipts.append(f"Attached {len(photo_paths)} photo(s) to {act.get('item_label') or act['item_id']}")

        elif atype == "upsert_entity":
            cname = (
                act.get("party_or_karigar")
                or act.get("followup_name")
                or act.get("customer_name")
                or ""
            ).strip()
            etype = (act.get("entity_type") or "supplier").strip().lower()
            if etype not in ("customer", "supplier", "karigar", "material"):
                etype = "supplier"
            enotes = (act.get("notes") or act.get("action_note") or act.get("summary") or "").strip()
            ephone = act.get("phone")
            if cname and enotes:
                if etype in ("customer", "supplier") and zoho.get_settings().zoho_enabled:
                    z_c = await zoho.resolve_or_create_contact(
                        sb,
                        cname,
                        "vendor" if etype == "supplier" else "customer",
                        phone=ephone,
                        notes=enotes,
                    )
                    if z_c.get("status") in ("error", "stubbed", "ambiguous"):
                        zoho._upsert_entity_alias(sb, cname, cname, etype, phone=ephone, notes=enotes)
                else:
                    zoho._upsert_entity_alias(sb, cname, cname, etype, phone=ephone, notes=enotes)
                receipts.append(f"Remembered: {cname} ({etype}) — {enotes}")

    if pending.get("was_corrected") and pending.get("correction_text"):
        try:
            rule = gemini_router.distill_learning_rule(
                pending.get("prior_summary") or "",
                pending.get("correction_text") or "",
                actions,
            )
            if rule:
                sb.table("workshop_memory").insert(
                    {
                        "category": rule["category"],
                        "rule_text": rule["rule_text"],
                        "source": "auto_correction",
                    }
                ).execute()
        except Exception as exc:
            logger.warning("Failed distilling workshop_memory rule: %s", exc)

    return receipts, None


def _is_state_expired(state_row: dict[str, Any], max_age_minutes: int = 360) -> bool:
    created_raw = state_row.get("created_at")
    if not created_raw:
        return False
    try:
        created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).total_seconds() > max_age_minutes * 60
    except ValueError:
        return False


def _save_bot_msg_id(sb: Any, state_id: str, pending_payload: dict[str, Any], sent_resp: dict[str, Any]) -> None:
    bot_mid = (sent_resp.get("result") or {}).get("message_id")
    if bot_mid:
        sb.table("telegram_state").update(
            {"pending_actions": {**pending_payload, "bot_message_id": bot_mid}}
        ).eq("id", state_id).execute()


def _find_active_pending_state(
    sb: Any, chat_id: int, user_id: int | None, reply_to_msg_id: int | None
) -> dict[str, Any] | None:
    if not user_id:
        return None
    rows = (
        sb.table("telegram_state")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(5)
        .execute()
        .data
        or []
    )
    active = [r for r in rows if not _is_state_expired(r) and (r.get("pending_actions") or {}).get("actions") is not None]
    if reply_to_msg_id:
        for r in active:
            if (r.get("pending_actions") or {}).get("bot_message_id") == reply_to_msg_id:
                return r
    for r in active:
        pa = r.get("pending_actions") or {}
        if pa.get("awaiting_clarification") or pa.get("last_error"):
            return r
    return None


async def _handle_callback_query(
    sb: Any, cq: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    cq_id = cq["id"]
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    actor = cq.get("from", {}).get("first_name") or str(cq.get("from", {}).get("id", "telegram"))

    try:
        if ":" not in data:
            await telegram_api.answer_callback_query(cq_id, "Unknown action")
            return {"ok": True}

        parts = data.split(":", 2)
        action, state_id = parts[0], parts[1]
        extra_arg = parts[2] if len(parts) > 2 else ""
        row_res = (
            sb.table("telegram_state")
            .select("*")
            .eq("id", state_id)
            .limit(1)
            .execute()
        )
        if not row_res.data:
            await telegram_api.answer_callback_query(cq_id, "Action expired or not found.")
            return {"ok": True}

        state_row = row_res.data[0]
        if state_row["status"] != "pending":
            await telegram_api.answer_callback_query(
                cq_id, f"Already {state_row['status']}."
            )
            return {"ok": True}

        if action == "confirm" and _is_state_expired(state_row):
            sb.table("telegram_state").update({"status": "expired"}).eq("id", state_id).execute()
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="⏰ *Prompt Expired (>6h)* — please resend your message.",
                reply_markup={"inline_keyboard": []},
            )
            await telegram_api.answer_callback_query(cq_id, "Prompt expired (>6h).")
            return {"ok": True}

        if action == "confirm":
            pending_obj = state_row.get("pending_actions") or {}
            receipts, ambiguous = await asyncio.wait_for(
                _commit_pending_actions(sb, pending_obj, actor),
                timeout=20.0,
            )
            if ambiguous:
                pending_obj["contact_candidates"] = [
                    {"id": m["contact_id"], "name": m["contact_name"]}
                    for m in ambiguous["matches"]
                ]
                pending_obj["contact_target"] = {
                    "action_index": ambiguous["action_index"],
                    "name": ambiguous["party_name"],
                    "type": ambiguous["contact_type"],
                }
                sb.table("telegram_state").update({"pending_actions": pending_obj}).eq("id", state_id).execute()
                kb = _build_contact_picker_keyboard(
                    state_id, ambiguous["party_name"], ambiguous["matches"]
                )
                await telegram_api.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"🔎 *Multiple Zoho {ambiguous['contact_type'].title()}s Found for `{ambiguous['party_name']}`*\n"
                        "Tap the matching contact (or create a new one):"
                    ),
                    reply_markup=kb,
                )
                await telegram_api.answer_callback_query(cq_id, "Please pick the matching contact.")
                return {"ok": True}

            sb.table("telegram_state").update({"status": "confirmed"}).eq(
                "id", state_id
            ).execute()
            receipt_text = "\n".join(f"• {r}" for r in receipts) or "Logged."
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ *Confirmed & Saved*\n{receipt_text}",
                reply_markup={"inline_keyboard": []},
            )
            await telegram_api.answer_callback_query(cq_id, "Saved!")

        elif action == "pickcontact" and extra_arg:
            pending_obj = state_row.get("pending_actions") or {}
            candidates = pending_obj.get("contact_candidates") or []
            target = pending_obj.get("contact_target") or {}
            sel_id: str | None = None
            sel_name: str | None = None
            force_new = extra_arg == "new"
            if not force_new:
                idx = int(extra_arg)
                if 0 <= idx < len(candidates):
                    sel_id = candidates[idx]["id"]
                    sel_name = candidates[idx]["name"]

            # Case A: Supplier Bill disambiguation
            if pending_obj.get("bill_payload"):
                bp = pending_obj["bill_payload"]
                c_res = await zoho.resolve_or_create_contact(
                    sb,
                    bp["supplier_name"],
                    "vendor",
                    gstin=bp.get("gstin"),
                    force_create=force_new,
                    selected_contact_id=sel_id,
                    selected_contact_name=sel_name,
                )
                draft_res = await zoho.create_draft_bill(
                    supplier_name=c_res.get("contact_name") or bp["supplier_name"],
                    invoice_no=bp.get("invoice_no"),
                    bill_date=bp.get("bill_date"),
                    line_items=bp.get("line_items") or [],
                    notes=bp.get("notes") or "",
                    vendor_id=c_res.get("contact_id"),
                )
                sb.table("telegram_state").update({"status": "confirmed"}).eq("id", state_id).execute()
                z_status = (
                    f"Error: {draft_res['error']}"
                    if draft_res.get("error")
                    else f"Created Zoho Draft Bill ({c_res.get('status', 'resolved')})"
                )
                await telegram_api.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"🧾 *Supplier Bill Processed*\n"
                        f"• Supplier: *{c_res.get('contact_name') or bp['supplier_name']}*\n"
                        f"• Invoice: `{bp.get('invoice_no') or 'N/A'}` | Total: *₹{float(bp.get('total_amount') or 0):,.2f}*\n"
                        f"• Zoho: _{z_status}_"
                    ),
                    reply_markup={"inline_keyboard": []},
                )
                await telegram_api.answer_callback_query(cq_id, "Bill saved!")
                return {"ok": True}

            # Case B: Voice/text/payment-receipt actions disambiguation
            acts = pending_obj.get("actions") or []
            a_idx = int(target.get("action_index", 0))
            if 0 <= a_idx < len(acts):
                if force_new:
                    acts[a_idx]["force_create_contact"] = True
                elif sel_id:
                    acts[a_idx]["zoho_contact_id"] = sel_id
                    acts[a_idx]["zoho_contact_name"] = sel_name
            pending_obj["actions"] = acts
            receipts, ambiguous = await asyncio.wait_for(
                _commit_pending_actions(sb, pending_obj, actor),
                timeout=20.0,
            )
            if ambiguous:
                pending_obj["contact_candidates"] = [
                    {"id": m["contact_id"], "name": m["contact_name"]}
                    for m in ambiguous["matches"]
                ]
                pending_obj["contact_target"] = {
                    "action_index": ambiguous["action_index"],
                    "name": ambiguous["party_name"],
                    "type": ambiguous["contact_type"],
                }
                sb.table("telegram_state").update({"pending_actions": pending_obj}).eq("id", state_id).execute()
                kb = _build_contact_picker_keyboard(
                    state_id, ambiguous["party_name"], ambiguous["matches"]
                )
                await telegram_api.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"🔎 *Multiple Zoho {ambiguous['contact_type'].title()}s Found for `{ambiguous['party_name']}`*\n"
                        "Tap the matching contact (or create a new one):"
                    ),
                    reply_markup=kb,
                )
                await telegram_api.answer_callback_query(cq_id, "Pick next contact.")
                return {"ok": True}

            sb.table("telegram_state").update({"status": "confirmed"}).eq("id", state_id).execute()
            receipt_text = "\n".join(f"• {r}" for r in receipts) or "Logged."
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ *Confirmed & Saved*\n{receipt_text}",
                reply_markup={"inline_keyboard": []},
            )
            await telegram_api.answer_callback_query(cq_id, "Saved!")

        elif action == "cancel":
            sb.table("telegram_state").update({"status": "cancelled"}).eq(
                "id", state_id
            ).execute()
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ *Cancelled* — nothing was saved.",
                reply_markup={"inline_keyboard": []},
            )
            await telegram_api.answer_callback_query(cq_id, "Cancelled")

        elif action == "edit":
            await telegram_api.answer_callback_query(
                cq_id, "Reply with correction or open Dashboard."
            )
            edit_sent = await telegram_api.send_message(
                chat_id=chat_id,
                text="✏️ Reply with a corrected voice note/text, or open the Dashboard to edit directly.",
                reply_to_message_id=message_id,
            )
            pa = state_row.get("pending_actions") or {}
            _save_bot_msg_id(sb, state_id, {**pa, "awaiting_clarification": True}, edit_sent)

        elif action == "txntype" and extra_arg:
            chosen_type = extra_arg
            pending_obj = state_row.get("pending_actions") or {}
            acts = pending_obj.get("actions") or []
            for a in acts:
                if a.get("action_type") == "log_cash":
                    a["txn_type"] = chosen_type
                    a["direction"] = "in" if chosen_type == "customer_receipt" else "out"
                    a["needs_txn_clarification"] = False
            pending_obj["actions"] = acts
            sb.table("telegram_state").update({"pending_actions": pending_obj}).eq("id", state_id).execute()
            lines = "\n".join(f"• {a.get('summary', '')} [{chosen_type}]" for a in acts)
            kb, _ = _build_confirm_keyboard(state_id, acts)
            await telegram_api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"📝 *Please Confirm* ({chosen_type})\n\n{lines}",
                reply_markup=kb,
            )
            await telegram_api.answer_callback_query(cq_id, f"Selected {chosen_type}")

    except Exception as exc:
        logger.exception("Error handling callback_query %s", data)
        await _notify_admin_error(settings, exc, f"callback={data}")
        await telegram_api.answer_callback_query(cq_id, "⚠️ Error saving — reply to fix.")
        if chat_id and message_id:
            try:
                err_sent = await telegram_api.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ Could not save: `{str(exc)[:140]}`\n"
                        "Reply to this message with a correction (or tap ✅ Confirm All again) and I'll retry."
                    ),
                    reply_to_message_id=message_id,
                )
                if ":" in data:
                    _, sid = data.split(":", 1)[:2]
                    cur = sb.table("telegram_state").select("pending_actions").eq("id", sid).limit(1).execute().data
                    if cur:
                        pa = cur[0].get("pending_actions") or {}
                        _save_bot_msg_id(sb, sid, {**pa, "last_error": str(exc)[:180]}, err_sent)
            except Exception:
                logger.exception("Failed to send callback error notice to chat")

    return {"ok": True}


async def _notify_admin_error(settings: Settings, err: Exception, context: str) -> None:
    admin_id = settings.admin_telegram_user_id.strip() or (
        str(next(iter(settings.allowed_telegram_id_set)))
        if settings.allowed_telegram_id_set
        else ""
    )
    if not admin_id or not settings.telegram_bot_token:
        return
    try:
        await telegram_api.send_message(
            chat_id=admin_id,
            text=f"🚨 *Workshop Munim Worker Error*\nContext: `{context}`\nError: `{str(err)[:300]}`",
        )
    except Exception:
        logger.exception("Failed to send admin error alert")


async def process_telegram_update(update: dict[str, Any]) -> None:
    """Async worker for voice notes, photos, bills, and text Q&A."""
    settings = get_settings()
    try:
        await _process_telegram_update_inner(update, settings)
    except Exception as exc:
        logger.exception("Error processing Telegram update %s", update.get("update_id"))
        await _notify_admin_error(settings, exc, f"update_id={update.get('update_id')}")


async def _process_telegram_update_inner(
    update: dict[str, Any], settings: Settings
) -> None:
    sb = get_supabase_admin()
    update_id = update.get("update_id")
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    message_id = msg["message_id"]
    media_group_id = msg.get("media_group_id")
    caption = msg.get("caption") or ""
    text = msg.get("text") or ""
    group_scope = _resolve_group_scope(chat_id, settings)

    # Media-group batching for multi-photo bursts
    if media_group_id:
        await asyncio.sleep(1.5)
        siblings = (
            sb.table("telegram_state")
            .select("id,telegram_update_id,pending_actions")
            .eq("media_group_id", media_group_id)
            .order("telegram_update_id")
            .execute()
            .data
            or []
        )
        if siblings and siblings[0]["telegram_update_id"] != update_id:
            return
        file_ids = [
            s.get("pending_actions", {}).get("photo_file_id")
            for s in siblings
            if s.get("pending_actions", {}).get("photo_file_id")
        ]
    else:
        file_ids = []
        if msg.get("photo"):
            file_ids.append(msg["photo"][-1]["file_id"])

    # 1. Supplier Bill PDF, explicit bill photo, or auto-classified photo in Accounts/DM
    doc = msg.get("document")
    is_bill_caption = any(
        kw in caption.lower() for kw in ("bill", "invoice", "gst", "parchi", "kharch")
    )
    prefetched_photo_bytes: bytes | None = None
    if not doc and len(file_ids) == 1 and not media_group_id and not is_bill_caption and group_scope in ("accounts", "dm"):
        prefetched_photo_bytes, _ = await telegram_api.download_file_bytes(file_ids[0])
        cls_res = gemini_router.classify_accounts_photo(
            sb, file_bytes=prefetched_photo_bytes, mime_type="image/jpeg", caption=caption
        )
        if cls_res.photo_kind == "multiple_transactions":
            await telegram_api.send_message(
                chat_id=chat_id,
                text="⚠️ I see multiple transactions in this screenshot — please send one transaction per message/screenshot.",
                reply_to_message_id=message_id,
            )
            return
        if cls_res.photo_kind == "payment_receipt" and cls_res.payment_action:
            pact = cls_res.payment_action
            pact.action_type = "log_cash"
            dup_warn = ""
            if pact.amount_inr is not None and pact.party_or_karigar:
                today_iso = datetime.now(timezone.utc).date().isoformat()
                dup_rows = (
                    sb.table("cash_ledger")
                    .select("id,amount_inr,party_or_karigar")
                    .eq("amount_inr", round(float(pact.amount_inr), 2))
                    .ilike("party_or_karigar", f"%{pact.party_or_karigar}%")
                    .gte("created_at", f"{today_iso}T00:00:00Z")
                    .limit(1)
                    .execute()
                    .data
                )
                if dup_rows:
                    dup_warn = "\n⚠️ *Possible duplicate* (same party & amount logged today)"
            state_row = (
                sb.table("telegram_state")
                .select("id")
                .eq("telegram_update_id", update_id)
                .limit(1)
                .execute()
                .data[0]
            )
            dumped = [pact.model_dump()]
            pending_pay = {"actions": dumped}
            sb.table("telegram_state").update(
                {"pending_actions": pending_pay}
            ).eq("id", state_row["id"]).execute()
            kb, needs_pick = _build_confirm_keyboard(str(state_row["id"]), dumped)
            title = "🤔 *Payment Receipt — Which Category?*" if needs_pick else "💳 *Payment Receipt Detected*"
            sent_pay = await telegram_api.send_message(
                chat_id=chat_id,
                text=f"{title}{dup_warn}\n• {pact.summary}",
                reply_markup=kb,
                reply_to_message_id=message_id,
            )
            _save_bot_msg_id(sb, str(state_row["id"]), pending_pay, sent_pay)
            return
        if cls_res.photo_kind == "supplier_bill":
            is_bill_caption = True

    if doc or (file_ids and is_bill_caption and not media_group_id):
        target_file_id = doc["file_id"] if doc else file_ids[0]
        raw_bytes = prefetched_photo_bytes or (await telegram_api.download_file_bytes(target_file_id))[0]
        mime_type = (doc or {}).get("mime_type") or "image/jpeg"
        ext = "pdf" if mime_type == "application/pdf" else "webp"

        extracted = gemini_router.extract_supplier_bill(
            sb, file_bytes=raw_bytes, mime_type=mime_type, caption=caption
        )
        dup = await zoho.check_duplicate_bill(
            sb,
            supplier_name=extracted.supplier_name,
            invoice_no=extracted.invoice_no,
            total_amount=extracted.total_amount,
        )
        if dup:
            await telegram_api.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ *Possible Duplicate Bill Detected* ({dup['source']})\n"
                    f"Supplier: `{extracted.supplier_name}`\n"
                    f"Invoice: `{extracted.invoice_no}` | Amount: ₹{extracted.total_amount:,.2f}\n"
                    "Skipped Zoho Draft creation."
                ),
                reply_to_message_id=message_id,
            )
            return

        now = datetime.now(timezone.utc)
        safe_vendor = "".join(c if c.isalnum() else "_" for c in extracted.supplier_name)[:30]
        safe_inv = "".join(c if c.isalnum() else "_" for c in (extracted.invoice_no or "noinv"))[:20]
        obj_path = f"supplier_bills/{now:%Y-%m}/{now:%Y%m%d}_{safe_vendor}_{safe_inv}.{ext}"
        if ext == "webp":
            storage.upload_image_as_webp(obj_path, raw_bytes)
        else:
            storage.upload_bytes(obj_path, raw_bytes, content_type=mime_type)

        c_res = await zoho.resolve_or_create_contact(
            sb,
            extracted.supplier_name,
            "vendor",
            gstin=extracted.gstin,
        )
        if c_res.get("status") == "ambiguous":
            state_row = (
                sb.table("telegram_state")
                .select("id")
                .eq("telegram_update_id", update_id)
                .limit(1)
                .execute()
                .data[0]
            )
            pending_bill = {
                "bill_payload": {
                    "supplier_name": extracted.supplier_name,
                    "gstin": extracted.gstin,
                    "invoice_no": extracted.invoice_no,
                    "bill_date": extracted.bill_date,
                    "total_amount": extracted.total_amount,
                    "line_items": [li.model_dump() for li in extracted.line_items],
                    "notes": extracted.notes or caption,
                },
                "contact_candidates": [
                    {"id": m["contact_id"], "name": m["contact_name"]}
                    for m in c_res["matches"]
                ],
                "contact_target": {
                    "name": extracted.supplier_name,
                    "type": "vendor",
                },
            }
            sb.table("telegram_state").update({"pending_actions": pending_bill}).eq(
                "id", state_row["id"]
            ).execute()
            kb = _build_contact_picker_keyboard(
                str(state_row["id"]), extracted.supplier_name, c_res["matches"]
            )
            await telegram_api.send_message(
                chat_id=chat_id,
                text=(
                    f"🧾 *Bill Parsed — Pick Matching Zoho Vendor*\n"
                    f"• Detected: `{extracted.supplier_name}` | Total: *₹{extracted.total_amount:,.2f}*\n"
                    "Multiple matching vendors found in Zoho Books:"
                ),
                reply_markup=kb,
                reply_to_message_id=message_id,
            )
            return

        draft_res = await zoho.create_draft_bill(
            supplier_name=c_res.get("contact_name") or extracted.supplier_name,
            invoice_no=extracted.invoice_no,
            bill_date=extracted.bill_date,
            line_items=[li.model_dump() for li in extracted.line_items],
            notes=extracted.notes or caption,
            vendor_id=c_res.get("contact_id"),
        )
        new_vendor_note = " (+ New Zoho Vendor created)" if c_res.get("status") == "created" else ""
        zoho_status = (
            "Logged locally (Zoho Wave 2 pending)"
            if draft_res.get("stubbed")
            else (
                f"Error: {draft_res['error']}"
                if draft_res.get("error")
                else f"Created Zoho Draft Bill{new_vendor_note} (staff to verify GST/HSN)"
            )
        )
        await telegram_api.send_message(
            chat_id=chat_id,
            text=(
                f"🧾 *Supplier Bill Processed*\n"
                f"• Supplier: *{extracted.supplier_name}*\n"
                f"• Invoice: `{extracted.invoice_no or 'N/A'}` | Date: `{extracted.bill_date or 'N/A'}`\n"
                f"• Total: *₹{extracted.total_amount:,.2f}* (GST: ₹{extracted.gst_amount:,.2f})\n"
                f"• Zoho: _{zoho_status}_"
            ),
            reply_to_message_id=message_id,
        )
        return

    # 2. Multi-photo or single design photo capture
    if file_ids:
        uploaded_paths: list[str] = []
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for idx, fid in enumerate(file_ids):
            raw_bytes, _ = await telegram_api.download_file_bytes(fid)
            obj_path = f"projects/inbox/{now_ts}_{update_id}_{idx}.webp"
            storage.upload_image_as_webp(obj_path, raw_bytes)
            uploaded_paths.append(obj_path)

        analysis = gemini_router.analyze_message_with_gemini(
            sb,
            text=f"User uploaded {len(uploaded_paths)} reference photo(s). Caption: '{caption}'. Match to an active project/item with action_type='attach_photos'.",
            group_scope=group_scope,
        )
        state_row = (
            sb.table("telegram_state")
            .select("id")
            .eq("telegram_update_id", update_id)
            .limit(1)
            .execute()
            .data[0]
        )
        pending_payload = {
            "actions": [a.model_dump() for a in analysis.actions],
            "uploaded_photo_paths": uploaded_paths,
        }
        sb.table("telegram_state").update({"pending_actions": pending_payload}).eq(
            "id", state_row["id"]
        ).execute()

        summary_lines = "\n".join(f"• {a.summary}" for a in analysis.actions) or (
            f"• Uploaded {len(uploaded_paths)} photo(s) (specify item in Dashboard if unmatched)"
        )
        kb = {
            "inline_keyboard": [[
                {"text": "✅ Confirm All", "callback_data": f"confirm:{state_row['id']}"},
                {"text": "✏️ Edit", "callback_data": f"edit:{state_row['id']}"},
                {"text": "❌ Cancel", "callback_data": f"cancel:{state_row['id']}"},
            ]]
        }
        await telegram_api.send_message(
            chat_id=chat_id,
            text=f"📸 *Design Photos Ready ({len(uploaded_paths)})*\n{summary_lines}",
            reply_markup=kb,
            reply_to_message_id=message_id,
        )
        return

    # 3. Voice note or text message (with reply-to-edit / clarification thread support)
    audio_bytes: bytes | None = None
    audio_mime = "audio/ogg"
    voice = msg.get("voice") or msg.get("audio")
    if voice:
        audio_bytes, _ = await telegram_api.download_file_bytes(voice["file_id"])
        audio_mime = voice.get("mime_type") or "audio/ogg"

    if not audio_bytes and not text:
        return

    user_id = (msg.get("from") or {}).get("id")
    reply_to_mid = (msg.get("reply_to_message") or {}).get("message_id")
    prior_state = _find_active_pending_state(sb, chat_id, user_id, reply_to_mid)
    prior_pa = (prior_state.get("pending_actions") or {}) if prior_state else {}
    prior_messages = list(prior_pa.get("prior_messages") or [])
    if prior_state and not prior_messages and prior_state.get("raw_transcript"):
        prior_messages.append(prior_state["raw_transcript"])
    prior_ctx = (
        {
            "prior_actions": prior_pa.get("actions", []),
            "prior_messages": prior_messages,
            "bot_prompt": prior_pa.get("bot_prompt"),
            "last_error": prior_pa.get("last_error"),
        }
        if prior_state
        else None
    )

    result = gemini_router.analyze_message_with_gemini(
        sb,
        text=text or None,
        audio_bytes=audio_bytes,
        audio_mime_type=audio_mime,
        group_scope=group_scope,
        prior_context=prior_ctx,
    )

    if result.intent in ("question", "draft_customer_message", "unknown"):
        reply_text = result.answer_text or result.transcript_summary
        await telegram_api.send_message(
            chat_id=chat_id,
            text=reply_text,
            reply_to_message_id=message_id,
        )
        return

    cur_row = (
        sb.table("telegram_state")
        .select("id")
        .eq("telegram_update_id", update_id)
        .limit(1)
        .execute()
        .data[0]
    )
    target_state_id = str(prior_state["id"]) if prior_state else str(cur_row["id"])
    if prior_state:
        sb.table("telegram_state").update({"status": "cancelled"}).eq("id", cur_row["id"]).execute()

    dumped_actions = [a.model_dump() for a in result.actions]
    accumulated_msgs = [*prior_messages, text or result.transcript_summary]
    correction_meta: dict[str, Any] = {"prior_messages": accumulated_msgs}
    if prior_state and text:
        correction_meta["was_corrected"] = True
        correction_meta["correction_text"] = text
        correction_meta["prior_summary"] = "; ".join(
            a.get("summary", "") for a in (prior_pa.get("actions") or []) if a.get("summary")
        )

    if result.intent == "clarification_needed":
        question_text = result.answer_text or "Could you clarify the missing details?"
        clarify_payload = {
            "actions": dumped_actions,
            "awaiting_clarification": True,
            "bot_prompt": question_text,
            **correction_meta,
        }
        sb.table("telegram_state").update({"pending_actions": clarify_payload}).eq("id", target_state_id).execute()
        sent_q = await telegram_api.send_message(
            chat_id=chat_id,
            text=f"❓ *Quick Clarification*\n{question_text}",
            reply_to_message_id=message_id,
        )
        _save_bot_msg_id(sb, target_state_id, clarify_payload, sent_q)
        return

    # Mutation intent -> require 1-tap inline confirmation
    confirm_payload = {"actions": dumped_actions, **correction_meta}
    sb.table("telegram_state").update({"pending_actions": confirm_payload}).eq("id", target_state_id).execute()

    lines = "\n".join(
        f"• {a.summary}" for a in result.actions
    ) or f"• {result.transcript_summary}"
    kb, needs_pick = _build_confirm_keyboard(target_state_id, dumped_actions)
    header = "🤔 *Which transaction type is this?*" if needs_pick else "📝 *Please Confirm*"
    sent_c = await telegram_api.send_message(
        chat_id=chat_id,
        text=f"{header}\n_{result.transcript_summary}_\n\n{lines}",
        reply_markup=kb,
        reply_to_message_id=message_id,
    )
    _save_bot_msg_id(sb, target_state_id, confirm_payload, sent_c)


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    _verify_webhook_secret(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    )
    update: dict[str, Any] = await request.json()
    update_id = update.get("update_id")
    if update_id is None:
        return {"ok": True}

    user_id, chat_id = _extract_user_and_chat(update)
    if (
        settings.allowed_telegram_id_set
        and user_id not in settings.allowed_telegram_id_set
    ):
        logger.warning("Ignored update_id=%s user_id=%s chat_id=%s reason=unauthorized_user", update_id, user_id, chat_id)
        return {"ok": True, "ignored": "unauthorized_user"}

    sb = get_supabase_admin()

    if "callback_query" in update:
        return await _handle_callback_query(sb, update["callback_query"], settings)

    msg = update.get("message") or update.get("edited_message") or {}
    reply_mid = (msg.get("reply_to_message") or {}).get("message_id")
    if not _should_process_group_text(msg) and not _find_active_pending_state(sb, chat_id or 0, user_id, reply_mid):
        logger.warning("Ignored update_id=%s user_id=%s chat_id=%s reason=casual_group_chatter", update_id, user_id, chat_id)
        return {"ok": True, "ignored": "casual_group_chatter"}

    media_group_id = msg.get("media_group_id")
    photo_file_id = msg["photo"][-1]["file_id"] if msg.get("photo") else None

    try:
        sb.table("telegram_state").insert(
            {
                "telegram_update_id": update_id,
                "chat_id": chat_id or 0,
                "user_id": user_id or 0,
                "media_group_id": media_group_id,
                "pending_actions": (
                    {"photo_file_id": photo_file_id} if photo_file_id else {}
                ),
                "status": "pending",
            }
        ).execute()
    except Exception as exc:
        err_msg = str(exc).lower()
        if "23505" in err_msg or "duplicate key" in err_msg or "unique" in err_msg:
            return {"ok": True, "deduped": True}
        logger.exception("Failed inserting telegram_state for update_id=%s", update_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist webhook state.",
        ) from exc

    if settings.cloud_tasks_enabled:
        _dispatch_to_cloud_tasks(update, settings)
    else:
        background_tasks.add_task(process_telegram_update, update)

    return {"ok": True}


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
    update: dict[str, Any] = await request.json()
    await process_telegram_update(update)
    return {"ok": True}
