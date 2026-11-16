"""Guarded, Idempotent Step Executors + 1-Tap [↩️ Undo] + Anti-Spam Handoff Pings.

The LLM never executes raw SQL or HTTP mutations directly. Instead, AgentCore
compiles validated intents into CheckpointedSteps executed here:
  - Production Lifecycle moves: auto-commit immediately, record reversible stage_events,
    apply smart defaults (Ansh on check_stock, Shreyansh on qc_done), fire operator-only
    Telegram handoff pings, and return an undoable receipt.
  - Customer/Entity Onboarding & Munim Zoho Pushes: confirm-gated, step-checkpointed
    with deterministic Zoho reference_number idempotency and Smart Auto-Bridge updates.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from app.services import telegram_api, zoho
from app.services.goal_primitives import (
    IST,
    ITEM_EXECUTION_STAGES,
    MATERIAL_TRACK_STAGES,
    PAYMENT_TRACK_STAGES,
    PROJECT_PIPELINE_STAGES,
    build_stage_undo_snapshot,
    format_inr,
    make_zoho_reference_number,
    merge_unique_str_list,
)

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# 1. Production Lifecycle Step Executors (Reversible via stage_events)
# ---------------------------------------------------------------------------
async def transition_project_pipeline(
    sb: Any,
    *,
    project_id: str,
    to_stage: str,
    actor_person_id: str | None = None,
    estimator_ids: list[str] | None = None,
    sizing_mistri_id: str | None = None,
    boq_scope_changed: bool | None = None,
    boq_scope_note: str | None = None,
    source_channel: str = "web",
    heard_transcript: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if to_stage not in PROJECT_PIPELINE_STAGES:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": f"Invalid pipeline stage '{to_stage}'."},
        }

    rows = sb.table("projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": "Order not found."},
        }
    prj = rows[0]
    prior_snap = build_stage_undo_snapshot(track="pipeline", row_before=prj)
    dur_hrs = _compute_duration_hours(prj.get("pipeline_stage_at"))

    patch: dict[str, Any] = {"pipeline_stage": to_stage}
    assigned_for_event: list[str] = []

    if to_stage == "design" and actor_person_id and not prj.get("designed_by_id"):
        patch["designed_by_id"] = actor_person_id
    if estimator_ids is not None:
        patch["estimator_ids"] = estimator_ids
    if to_stage == "estimating":
        eff_est = estimator_ids if estimator_ids is not None else list(prj.get("estimator_ids") or [])
        patch["current_owner_ids"] = eff_est
        assigned_for_event = eff_est
    elif to_stage == "site_sizing_boq":
        if sizing_mistri_id:
            patch["sizing_mistri_id"] = sizing_mistri_id
            assigned_for_event = [sizing_mistri_id]
        if prj.get("payment_stage") == "advance_pending":
            patch["payment_stage"] = "advance_in"
    if boq_scope_changed is not None:
        patch["boq_scope_changed"] = boq_scope_changed
    if boq_scope_note is not None:
        patch["boq_scope_note"] = boq_scope_note

    undo_ids: list[str] = []

    # Smart Default on Execution: auto-kickoff Ansh's Material Track ('check_stock')
    ansh_ping_needed = False
    ansh_id: str | None = None
    if to_stage == "execution" and prj.get("material_stage") == "not_started":
        ansh_id = prj.get("material_owner_id") or get_default_person_id(sb, "is_default_purchase")
        mat_prior = build_stage_undo_snapshot(track="material", row_before=prj)
        patch["material_stage"] = "check_stock"
        if ansh_id:
            patch["material_owner_id"] = ansh_id
            ansh_ping_needed = True
        mat_ev = (
            sb.table("stage_events")
            .insert(
                {
                    "project_id": project_id,
                    "track": "material",
                    "from_stage": prj.get("material_stage"),
                    "to_stage": "check_stock",
                    "prior_snapshot": mat_prior,
                    "assigned_people_ids": [ansh_id] if ansh_id else [],
                    "actor_person_id": actor_person_id,
                    "source_channel": source_channel,
                    "heard_transcript": heard_transcript,
                    "note": "Auto-started Material Track on Execution",
                    "is_undone": False,
                }
            )
            .execute()
            .data
            or []
        )
        if mat_ev:
            undo_ids.append(str(mat_ev[0]["id"]))

    sb.table("projects").update(patch).eq("id", project_id).execute()

    ev_rows = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "track": "pipeline",
                "from_stage": prj.get("pipeline_stage"),
                "to_stage": to_stage,
                "prior_snapshot": prior_snap,
                "assigned_people_ids": assigned_for_event,
                "actor_person_id": actor_person_id,
                "source_channel": source_channel,
                "heard_transcript": heard_transcript,
                "duration_hours": dur_hrs,
                "note": note,
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    if ev_rows:
        undo_ids.append(str(ev_rows[0]["id"]))

    # Smart Auto-Bridge to Zoho Books Standard when deal converts
    if to_stage in ("site_sizing_boq", "execution"):
        await zoho.ensure_project_zoho_bridge(sb, project_id)

    # Anti-spam operator handoff pings
    if to_stage == "estimating" and assigned_for_event:
        await _notify_operator_handoff(
            sb,
            assigned_for_event,
            actor_person_id,
            f"📐 *Estimating*: `{prj['project_code']}` — *{prj['customer_name']}* ({prj.get('rough_scope') or 'See order'})",
        )
    if ansh_ping_needed and ansh_id:
        await _notify_operator_handoff(
            sb,
            [ansh_id],
            actor_person_id,
            f"📦 *Check Stock*: `{prj['project_code']}` — *{prj['customer_name']}* entered Execution.",
        )

    stage_label = to_stage.replace("_", " ").title()
    extra_msg = " (+ Ansh queued for Check Stock)" if ansh_ping_needed else ""
    return {
        "ok": True,
        "undo_event_ids": undo_ids,
        "receipt": f"`{prj['project_code']}` *{prj['customer_name']}* → *{stage_label}*{extra_msg}",
    }


async def transition_material_track(
    sb: Any,
    *,
    project_id: str,
    to_stage: str,
    actor_person_id: str | None = None,
    material_owner_id: str | None = None,
    source_channel: str = "web",
    heard_transcript: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if to_stage not in MATERIAL_TRACK_STAGES:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": f"Invalid material stage '{to_stage}'."},
        }
    rows = sb.table("projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Order not found."}}
    prj = rows[0]
    prior_snap = build_stage_undo_snapshot(track="material", row_before=prj)
    owner_id = material_owner_id or prj.get("material_owner_id") or get_default_person_id(sb, "is_default_purchase")

    patch: dict[str, Any] = {"material_stage": to_stage}
    if owner_id:
        patch["material_owner_id"] = owner_id
    sb.table("projects").update(patch).eq("id", project_id).execute()

    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "track": "material",
                "from_stage": prj.get("material_stage"),
                "to_stage": to_stage,
                "prior_snapshot": prior_snap,
                "assigned_people_ids": [owner_id] if owner_id else [],
                "actor_person_id": actor_person_id,
                "source_channel": source_channel,
                "heard_transcript": heard_transcript,
                "duration_hours": _compute_duration_hours(prj.get("material_stage_at")),
                "note": note,
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )
    return {
        "ok": True,
        "undo_event_ids": [str(ev[0]["id"])] if ev else [],
        "receipt": f"Material (`{prj['customer_name']}`) → *{to_stage.replace('_', ' ').title()}*",
    }


async def upsert_or_transition_item(
    sb: Any,
    *,
    project_id: str,
    item_id: str | None = None,
    item_name: str | None = None,
    to_stage: str = "in_shop",
    production_mistri_id: str | None = None,
    assembly_mistri_id: str | None = None,
    qc_passed_by_id: str | None = None,
    dimensions: str | None = None,
    specs_notes: str | None = None,
    actor_person_id: str | None = None,
    source_channel: str = "web",
    heard_transcript: str | None = None,
) -> dict[str, Any]:
    if to_stage not in ITEM_EXECUTION_STAGES:
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": f"Invalid item stage '{to_stage}'."},
        }

    prj_rows = sb.table("projects").select("id,project_code,customer_name,payment_stage").eq("id", project_id).limit(1).execute().data or []
    if not prj_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Parent order not found."}}
    prj = prj_rows[0]

    # Lookup Mistri display name for crisp receipt
    def _person_name(pid: str | None) -> str:
        if not pid:
            return ""
        p = sb.table("people").select("full_name").eq("id", pid).limit(1).execute().data
        return p[0]["full_name"] if p else ""

    if not item_id and item_name:
        existing_items = (
            sb.table("project_items")
            .select("*")
            .eq("project_id", project_id)
            .ilike("item_name", item_name.strip())
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing_items:
            item_id = existing_items[0]["id"]

    if not item_id:
        if not item_name or not item_name.strip():
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Item name is required (e.g., Kitchen, Master Bed)."}}
        m_name = _person_name(production_mistri_id)
        ins = (
            sb.table("project_items")
            .insert(
                {
                    "project_id": project_id,
                    "item_name": item_name.strip(),
                    "stage": to_stage,
                    "dimensions": dimensions,
                    "specs_notes": specs_notes,
                    "production_mistri_id": production_mistri_id,
                    "assembly_mistri_id": assembly_mistri_id,
                    "assigned_karigar": m_name or None,
                }
            )
            .execute()
            .data
            or []
        )
        created_item = ins[0]
        ev = (
            sb.table("stage_events")
            .insert(
                {
                    "project_id": project_id,
                    "item_id": created_item["id"],
                    "track": "item",
                    "from_stage": None,
                    "to_stage": to_stage,
                    "prior_snapshot": {"_created": True},
                    "assigned_people_ids": [p for p in (production_mistri_id, assembly_mistri_id) if p],
                    "actor_person_id": actor_person_id,
                    "source_channel": source_channel,
                    "heard_transcript": heard_transcript,
                    "is_undone": False,
                }
            )
            .execute()
            .data
            or []
        )
        who = f" (Shop: {m_name})" if m_name else ""
        return {
            "ok": True,
            "item_id": created_item["id"],
            "undo_event_ids": [str(ev[0]["id"])] if ev else [],
            "receipt": f"Added *{created_item['item_name']}* on `{prj['customer_name']}` → *{to_stage.replace('_', ' ').title()}*{who}",
        }

    cur_rows = sb.table("project_items").select("*").eq("id", item_id).limit(1).execute().data or []
    if not cur_rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Furniture item not found."}}
    cur = cur_rows[0]
    prior_snap = build_stage_undo_snapshot(track="item", row_before=cur)

    patch: dict[str, Any] = {"stage": to_stage}
    # Preserve production_mistri_id permanently unless explicitly changed!
    if production_mistri_id:
        patch["production_mistri_id"] = production_mistri_id
        patch["assigned_karigar"] = _person_name(production_mistri_id) or cur.get("assigned_karigar")
    if assembly_mistri_id:
        patch["assembly_mistri_id"] = assembly_mistri_id
    if to_stage == "qc_done":
        qc_id = qc_passed_by_id or actor_person_id or get_default_person_id(sb, "is_default_qc")
        if qc_id:
            patch["qc_passed_by_id"] = qc_id
    if dimensions:
        patch["dimensions"] = dimensions
    if specs_notes:
        patch["specs_notes"] = specs_notes

    sb.table("project_items").update(patch).eq("id", item_id).execute()

    assigned_ids = [p for p in (production_mistri_id, assembly_mistri_id, patch.get("qc_passed_by_id")) if p]
    ev = (
        sb.table("stage_events")
        .insert(
            {
                "project_id": project_id,
                "item_id": item_id,
                "track": "item",
                "from_stage": cur.get("stage"),
                "to_stage": to_stage,
                "prior_snapshot": prior_snap,
                "assigned_people_ids": assigned_ids,
                "actor_person_id": actor_person_id,
                "source_channel": source_channel,
                "heard_transcript": heard_transcript,
                "duration_hours": _compute_duration_hours(cur.get("stage_entered_at")),
                "is_undone": False,
            }
        )
        .execute()
        .data
        or []
    )

    pay_gate_warn = ""
    if to_stage == "packed_ready" and prj.get("payment_stage") != "payment_complete":
        pay_gate_warn = f" _(⚠️ Payment: {prj.get('payment_stage', '').replace('_', ' ').title()})_"

    mistri_note = ""
    if assembly_mistri_id:
        mistri_note = f" (Site: {_person_name(assembly_mistri_id)})"
    elif production_mistri_id:
        mistri_note = f" (Shop: {_person_name(production_mistri_id)})"

    return {
        "ok": True,
        "item_id": item_id,
        "undo_event_ids": [str(ev[0]["id"])] if ev else [],
        "receipt": f"*{cur['item_name']}* (`{prj['customer_name']}`) → *{to_stage.replace('_', ' ').title()}*{mistri_note}{pay_gate_warn}",
    }


def undo_stage_events(
    sb: Any, event_ids: list[str], actor_person_id: str | None = None
) -> list[str]:
    """Revert one or more stage_events in reverse order (1-tap [↩️ Undo])."""
    if not event_ids:
        return []
    events_raw = (
        sb.table("stage_events")
        .select("*")
        .in_("id", event_ids)
        .eq("is_undone", False)
        .execute()
        .data
        or []
    )
    order_pos = {eid: idx for idx, eid in enumerate(event_ids)}
    events = sorted(events_raw, key=lambda e: order_pos.get(str(e["id"]), 0), reverse=True)
    reverted: list[str] = []
    for ev in events:
        snap = ev.get("prior_snapshot") or {}
        track = ev.get("track")
        if track == "item" and ev.get("item_id"):
            if snap.get("_created"):
                sb.table("project_items").delete().eq("id", ev["item_id"]).execute()
                reverted.append("Removed newly created item")
            else:
                sb.table("project_items").update(snap).eq("id", ev["item_id"]).execute()
                reverted.append(f"Item reverted to {snap.get('stage')}")
        elif track in ("pipeline", "material", "payment") and snap:
            sb.table("projects").update(snap).eq("id", ev["project_id"]).execute()
            reverted.append(f"Order {track} reverted")

        sb.table("stage_events").update(
            {
                "is_undone": True,
                "undone_at": datetime.now(timezone.utc).isoformat(),
                "undone_by_person_id": actor_person_id,
            }
        ).eq("id", ev["id"]).execute()
    return reverted


# ---------------------------------------------------------------------------
# 2. Customer Onboarding & Munim Financial Step Executors (Confirm-Gated)
# ---------------------------------------------------------------------------
def create_customer_and_lead(
    sb: Any,
    *,
    full_name: str,
    area_landmark: str,
    rough_scope: str,
    phone: str | None = None,
    quoted_total: float = 0.0,
    promised_delivery_date: str | None = None,
    existing_customer_id: str | None = None,
    actor_person_id: str | None = None,
) -> dict[str, Any]:
    """Create a canonical Customer (or link repeat Customer) + Supabase Lead Project.
    Dead leads never pollute Zoho Books until conversion!
    """
    cust_id = existing_customer_id
    cname = full_name.strip()
    area = area_landmark.strip()

    if not cust_id:
        ins_c = (
            sb.table("customers")
            .insert(
                {
                    "full_name": cname,
                    "area_landmark": area,
                    "phone": phone,
                }
            )
            .execute()
            .data
            or []
        )
        cust_id = ins_c[0]["id"]
    else:
        c_row = sb.table("customers").select("full_name,area_landmark,phone").eq("id", cust_id).limit(1).execute().data
        if c_row:
            cname = c_row[0]["full_name"]
            area = area or (c_row[0].get("area_landmark") or "")
            phone = phone or c_row[0].get("phone")

    now_ist = datetime.now(IST)
    code = f"PRJ-{now_ist:%y%m%d-%H%M%S}"
    display_cust = f"{cname} ({area})" if area and area.lower() not in cname.lower() else cname
    ins_p = (
        sb.table("projects")
        .insert(
            {
                "project_code": code,
                "customer_id": cust_id,
                "customer_name": display_cust,
                "site_area_landmark": area,
                "rough_scope": rough_scope.strip(),
                "phone": phone,
                "quoted_total": round(float(quoted_total or 0), 2),
                "promised_delivery_date": promised_delivery_date,
                "pipeline_stage": "lead",
                "current_owner_ids": [actor_person_id] if actor_person_id else [],
            }
        )
        .execute()
        .data
        or []
    )
    prj = ins_p[0]
    sb.table("stage_events").insert(
        {
            "project_id": prj["id"],
            "track": "pipeline",
            "from_stage": None,
            "to_stage": "lead",
            "actor_person_id": actor_person_id,
            "note": f"Scope: {rough_scope.strip()}",
        }
    ).execute()

    return {
        "ok": True,
        "customer_id": cust_id,
        "project_id": prj["id"],
        "project_code": code,
        "receipt": f"New Lead `{code}`: *{display_cust}* — Scope: _{rough_scope.strip()}_",
    }


async def execute_munim_financial_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    entry_kind: str,  # 'customer_receipt' | 'vendor_payment' | 'expense' | 'float_transfer'
    amount_inr: float,
    payment_account_id: str,
    to_payment_account_id: str | None = None,
    party_name: str = "",
    project_id: str | None = None,
    zoho_contact_id: str | None = None,
    txn_date: str | None = None,
    notes: str = "",
    actor_label: str = "Munim",
) -> dict[str, Any]:
    """Idempotent Munim -> Zoho Books Standard push + Smart Auto-Bridge Payment Track sync."""
    ref_no = make_zoho_reference_number(goal_id, step_index)

    # 1. Push to Zoho Books (with reference_number dedup + Reporting Tag bridge)
    z_res = await zoho.push_management_entry_to_zoho(
        sb,
        entry_kind=entry_kind,
        amount_inr=amount_inr,
        payment_account_id=payment_account_id,
        to_payment_account_id=to_payment_account_id,
        reference_number=ref_no,
        txn_date=txn_date,
        party_name=party_name,
        zoho_contact_id=zoho_contact_id,
        project_id=project_id,
        notes=notes,
        actor_label=actor_label,
    )
    if not z_res.get("ok"):
        return z_res

    # 2. Smart Auto-Bridge: Customer receipt automatically updates Production's Payment Track!
    pay_stage_note = ""
    if entry_kind == "customer_receipt" and project_id:
        p_rows = (
            sb.table("projects")
            .select("id,quoted_total,paid_cache_inr,payment_stage")
            .eq("id", project_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if p_rows:
            p = p_rows[0]
            new_paid = round(float(p.get("paid_cache_inr") or 0) + float(amount_inr), 2)
            quoted = float(p.get("quoted_total") or 0)
            if quoted > 0 and new_paid >= quoted:
                new_pstage = "payment_complete"
            elif p.get("payment_stage") == "advance_pending":
                new_pstage = "advance_in"
            else:
                new_pstage = "partial_paid" if new_paid > 0 else p.get("payment_stage")

            if new_pstage in PAYMENT_TRACK_STAGES:
                sb.table("projects").update(
                    {"paid_cache_inr": new_paid, "payment_stage": new_pstage}
                ).eq("id", project_id).execute()
                pay_stage_note = f" · Order Payment → *{new_pstage.replace('_', ' ').title()}*"

    acc_row = sb.table("payment_accounts").select("display_name").eq("id", payment_account_id).limit(1).execute().data
    acc_label = acc_row[0]["display_name"] if acc_row else "Account"
    arrow = "🟢 IN" if entry_kind == "customer_receipt" else ("🔄 TRANSFER" if entry_kind == "float_transfer" else "🔴 OUT")
    z_badge = "Zoho Stub" if z_res.get("stubbed") else f"Zoho `{z_res.get('zoho_type')}`"

    return {
        "ok": True,
        "status_code": 200,
        "error_kind": "none",
        "zoho_id": z_res.get("zoho_id"),
        "reference_number": ref_no,
        "receipt": f"{arrow} *{format_inr(amount_inr)}* ({acc_label}) — {party_name or entry_kind} [{z_badge}]{pay_stage_note}",
    }


# ---------------------------------------------------------------------------
# 3. Master Data Spine & Order Metadata CRUD + Cascades + Undo
# ---------------------------------------------------------------------------
def _format_customer_order_display_name(full_name: str, area_landmark: str | None) -> str:
    cname = full_name.strip()
    area = (area_landmark or "").strip()
    if area and area.lower() not in cname.lower():
        return f"{cname} ({area})"
    return cname


def create_standalone_customer(
    sb: Any,
    *,
    full_name: str,
    area_landmark: str | None = None,
    phone: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a Customer record in the Master Spine without creating a Lead order or Zoho clutter."""
    cname = full_name.strip()
    if not cname:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer full name is required."}}
    payload: dict[str, Any] = {
        "full_name": cname,
        "area_landmark": (area_landmark or "").strip() or None,
        "phone": (phone or "").strip() or None,
        "aliases": merge_unique_str_list([], aliases),
        "notes": (notes or "").strip() or None,
    }
    ins = sb.table("customers").insert(payload).execute().data or []
    cust = ins[0]
    area_txt = f" ({cust['area_landmark']})" if cust.get("area_landmark") else ""
    return {
        "ok": True,
        "customer_id": cust["id"],
        "customer": cust,
        "receipt": f"Added Customer *{cust['full_name']}*{area_txt}",
    }


async def update_customer_record(
    sb: Any,
    *,
    customer_id: str,
    full_name: str | None = None,
    area_landmark: str | None = None,
    phone: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    replace_lists_and_notes: bool = False,
) -> dict[str, Any]:
    """Update a Customer, cascade name/area/phone to active Orders, and sync Zoho if already linked."""
    rows = sb.table("customers").select("*").eq("id", customer_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer not found."}}
    cur = rows[0]

    prior_snapshot = {
        "full_name": cur.get("full_name"),
        "area_landmark": cur.get("area_landmark"),
        "phone": cur.get("phone"),
        "aliases": list(cur.get("aliases") or []),
        "notes": cur.get("notes"),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if full_name is not None and full_name.strip() and full_name.strip() != (cur.get("full_name") or ""):
        patch["full_name"] = full_name.strip()
        changes_desc.append(f"Name → *{patch['full_name']}*")
    if area_landmark is not None and area_landmark.strip() != (cur.get("area_landmark") or ""):
        patch["area_landmark"] = area_landmark.strip() or None
        changes_desc.append(f"Area → *{patch['area_landmark'] or '-'}*")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists_and_notes
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if notes is not None:
        new_notes = (
            (notes.strip() or None)
            if replace_lists_and_notes
            else (zoho.merge_notes_text(cur.get("notes"), notes) or None)
        )
        if new_notes != cur.get("notes"):
            patch["notes"] = new_notes
            changes_desc.append("Notes updated")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "customer_id": customer_id,
            "customer": cur,
            "receipt": f"No changes needed for *{cur['full_name']}*",
        }

    updated_rows = sb.table("customers").update(patch).eq("id", customer_id).execute().data or [cur]
    updated = updated_rows[0]

    # Cascade name / area / phone to active Customer Orders in Production
    cascaded_projects_before: list[dict[str, Any]] = []
    if any(k in patch for k in ("full_name", "area_landmark", "phone")):
        active_orders = (
            sb.table("projects")
            .select("id,customer_name,site_area_landmark,phone")
            .eq("customer_id", customer_id)
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        eff_name = updated.get("full_name") or cur["full_name"]
        eff_area = updated.get("area_landmark")
        order_patch: dict[str, Any] = {
            "customer_name": _format_customer_order_display_name(eff_name, eff_area),
        }
        if "area_landmark" in patch:
            order_patch["site_area_landmark"] = eff_area
        if "phone" in patch:
            order_patch["phone"] = updated.get("phone")

        for ord_row in active_orders:
            cascaded_projects_before.append(
                {
                    "id": ord_row["id"],
                    "patch": {
                        "customer_name": ord_row.get("customer_name"),
                        "site_area_landmark": ord_row.get("site_area_landmark"),
                        "phone": ord_row.get("phone"),
                    },
                }
            )
            sb.table("projects").update(order_patch).eq("id", ord_row["id"]).execute()

    # Sync to Zoho Books India ONLY if zoho_contact_id already exists (preserves lazy creation)
    zoho_cid = updated.get("zoho_contact_id") or cur.get("zoho_contact_id")
    zoho_synced = False
    if zoho_cid and any(k in patch for k in ("full_name", "phone", "notes")):
        zoho_synced = await zoho.sync_existing_zoho_contact(
            zoho_cid,
            name=patch.get("full_name"),
            phone=patch.get("phone") if "phone" in patch else None,
            replace_notes=(patch.get("notes") or "") if (replace_lists_and_notes and "notes" in patch) else None,
            notes_to_merge=notes if (not replace_lists_and_notes and "notes" in patch) else None,
        )

    entity_undo = {
        "table": "customers",
        "record_id": customer_id,
        "prior_snapshot": prior_snapshot,
        "cascaded_projects_before": cascaded_projects_before,
        "zoho_contact_id": zoho_cid if zoho_synced else None,
    }
    z_note = " (+ Zoho synced)" if zoho_synced else ""
    ord_note = f" (+ {len(cascaded_projects_before)} active order(s) updated)" if cascaded_projects_before else ""
    return {
        "ok": True,
        "customer_id": customer_id,
        "customer": updated,
        "entity_undo": entity_undo,
        "receipt": f"Customer *{updated['full_name']}*: {', '.join(changes_desc)}{ord_note}{z_note}",
    }


async def update_vendor_record(
    sb: Any,
    *,
    vendor_id: str,
    canonical_name: str | None = None,
    vendor_kind: str | None = None,
    phone: str | None = None,
    gstin: str | None = None,
    specialty: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    replace_lists_and_notes: bool = False,
) -> dict[str, Any]:
    """Update a Vendor/Contractor and sync to Zoho Books India if zoho_contact_id already exists."""
    rows = sb.table("vendors").select("*").eq("id", vendor_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Vendor not found."}}
    cur = rows[0]

    prior_snapshot = {
        "canonical_name": cur.get("canonical_name"),
        "vendor_kind": cur.get("vendor_kind"),
        "phone": cur.get("phone"),
        "gstin": cur.get("gstin"),
        "specialty": cur.get("specialty"),
        "aliases": list(cur.get("aliases") or []),
        "notes": cur.get("notes"),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if canonical_name is not None and canonical_name.strip() and canonical_name.strip() != (cur.get("canonical_name") or ""):
        patch["canonical_name"] = canonical_name.strip()
        changes_desc.append(f"Name → *{patch['canonical_name']}*")
    if vendor_kind in ("supplier", "contractor") and vendor_kind != cur.get("vendor_kind"):
        patch["vendor_kind"] = vendor_kind
        changes_desc.append(f"Kind → `{vendor_kind}`")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if gstin is not None and gstin.strip().upper() != (cur.get("gstin") or ""):
        patch["gstin"] = gstin.strip().upper() or None
        changes_desc.append(f"GSTIN → `{patch['gstin'] or '-'}`")
    if specialty is not None and specialty.strip() != (cur.get("specialty") or ""):
        patch["specialty"] = specialty.strip() or None
        changes_desc.append(f"Specialty → *{patch['specialty'] or '-'}*")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists_and_notes
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if notes is not None:
        new_notes = (
            (notes.strip() or None)
            if replace_lists_and_notes
            else (zoho.merge_notes_text(cur.get("notes"), notes) or None)
        )
        if new_notes != cur.get("notes"):
            patch["notes"] = new_notes
            changes_desc.append("Notes updated")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "vendor_id": vendor_id,
            "vendor": cur,
            "receipt": f"No changes needed for *{cur['canonical_name']}*",
        }

    updated_rows = sb.table("vendors").update(patch).eq("id", vendor_id).execute().data or [cur]
    updated = updated_rows[0]

    zoho_cid = updated.get("zoho_contact_id") or cur.get("zoho_contact_id")
    zoho_synced = False
    if zoho_cid and any(k in patch for k in ("canonical_name", "phone", "gstin", "notes")):
        zoho_synced = await zoho.sync_existing_zoho_contact(
            zoho_cid,
            name=patch.get("canonical_name"),
            phone=patch.get("phone") if "phone" in patch else None,
            gstin=patch.get("gstin") if "gstin" in patch else None,
            replace_notes=(patch.get("notes") or "") if (replace_lists_and_notes and "notes" in patch) else None,
            notes_to_merge=notes if (not replace_lists_and_notes and "notes" in patch) else None,
        )

    entity_undo = {
        "table": "vendors",
        "record_id": vendor_id,
        "prior_snapshot": prior_snapshot,
        "zoho_contact_id": zoho_cid if zoho_synced else None,
    }
    z_note = " (+ Zoho synced)" if zoho_synced else ""
    return {
        "ok": True,
        "vendor_id": vendor_id,
        "vendor": updated,
        "entity_undo": entity_undo,
        "receipt": f"Vendor *{updated['canonical_name']}*: {', '.join(changes_desc)}{z_note}",
    }


def update_person_record(
    sb: Any,
    *,
    person_id: str,
    full_name: str | None = None,
    person_type: str | None = None,
    role_tags: list[str] | None = None,
    phone: str | None = None,
    email: str | None = None,
    aliases: list[str] | None = None,
    payout_aliases: list[str] | None = None,
    is_active: bool | None = None,
    replace_lists: bool = False,
) -> dict[str, Any]:
    """Update a Person / Mistri record (phone, role_tags, voice aliases, bank payout aliases)."""
    rows = sb.table("people").select("*").eq("id", person_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Person not found."}}
    cur = rows[0]

    prior_snapshot = {
        "full_name": cur.get("full_name"),
        "person_type": cur.get("person_type"),
        "role_tags": list(cur.get("role_tags") or []),
        "phone": cur.get("phone"),
        "email": cur.get("email"),
        "aliases": list(cur.get("aliases") or []),
        "payout_aliases": list(cur.get("payout_aliases") or []),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if full_name is not None and full_name.strip() and full_name.strip() != (cur.get("full_name") or ""):
        patch["full_name"] = full_name.strip()
        changes_desc.append(f"Name → *{patch['full_name']}*")
    if person_type in ("operator", "salaried_karigar", "contractor_mistri") and person_type != cur.get("person_type"):
        patch["person_type"] = person_type
        if person_type == "operator":
            patch["can_login_prod"] = True
            patch["can_login_munim"] = True
        changes_desc.append(f"Type → `{person_type}`")
    if role_tags is not None:
        new_roles = (
            [str(r).strip() for r in role_tags if str(r).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("role_tags"), role_tags)
        )
        if new_roles != list(cur.get("role_tags") or []):
            patch["role_tags"] = new_roles
            changes_desc.append(f"Roles → `{', '.join(new_roles)}`")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if email is not None and email.strip() != (cur.get("email") or ""):
        patch["email"] = email.strip() or None
        changes_desc.append(f"Email → `{patch['email'] or '-'}`")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if payout_aliases is not None:
        new_pa = (
            [str(a).strip() for a in payout_aliases if str(a).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("payout_aliases"), payout_aliases)
        )
        if new_pa != list(cur.get("payout_aliases") or []):
            patch["payout_aliases"] = new_pa
            changes_desc.append(f"Bank Proxy → *{', '.join(new_pa) or '-'}*")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "person_id": person_id,
            "person": cur,
            "receipt": f"No changes needed for *{cur['full_name']}*",
        }

    updated_rows = sb.table("people").update(patch).eq("id", person_id).execute().data or [cur]
    updated = updated_rows[0]
    return {
        "ok": True,
        "person_id": person_id,
        "person": updated,
        "entity_undo": {
            "table": "people",
            "record_id": person_id,
            "prior_snapshot": prior_snapshot,
        },
        "receipt": f"Person *{updated['full_name']}*: {', '.join(changes_desc)}",
    }


def update_order_metadata(
    sb: Any,
    *,
    project_id: str,
    quoted_total: float | None = None,
    promised_delivery_date: str | None = None,
    rough_scope: str | None = None,
    site_area_landmark: str | None = None,
    phone: str | None = None,
    boq_scope_changed: bool | None = None,
    boq_scope_note: str | None = None,
) -> dict[str, Any]:
    """Update Order metadata (deal value ₹, delivery date, scope, area, phone, BOQ note) with Undo snapshot."""
    rows = sb.table("projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Order not found."}}
    cur = rows[0]

    prior_snapshot = {
        "quoted_total": cur.get("quoted_total"),
        "promised_delivery_date": cur.get("promised_delivery_date"),
        "rough_scope": cur.get("rough_scope"),
        "site_area_landmark": cur.get("site_area_landmark"),
        "phone": cur.get("phone"),
        "boq_scope_changed": cur.get("boq_scope_changed"),
        "boq_scope_note": cur.get("boq_scope_note"),
        "payment_stage": cur.get("payment_stage"),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if quoted_total is not None:
        new_q = round(float(quoted_total), 2)
        if new_q != round(float(cur.get("quoted_total") or 0), 2):
            patch["quoted_total"] = new_q
            changes_desc.append(f"Deal Value → *{format_inr(new_q)}*")
            paid = float(cur.get("paid_cache_inr") or 0)
            if new_q > 0 and paid >= new_q:
                patch["payment_stage"] = "payment_complete"
            elif paid > 0 and cur.get("payment_stage") == "payment_complete" and paid < new_q:
                patch["payment_stage"] = "partial_paid"
    if promised_delivery_date is not None and str(promised_delivery_date).strip() != str(cur.get("promised_delivery_date") or ""):
        patch["promised_delivery_date"] = str(promised_delivery_date).strip() or None
        changes_desc.append(f"Delivery Date → *{patch['promised_delivery_date'] or '-'}*")
    if rough_scope is not None and rough_scope.strip() != (cur.get("rough_scope") or ""):
        patch["rough_scope"] = rough_scope.strip() or None
        changes_desc.append(f"Scope → _{patch['rough_scope'] or '-'}_")
    if site_area_landmark is not None and site_area_landmark.strip() != (cur.get("site_area_landmark") or ""):
        patch["site_area_landmark"] = site_area_landmark.strip() or None
        changes_desc.append(f"Area → *{patch['site_area_landmark'] or '-'}*")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if boq_scope_changed is not None and bool(boq_scope_changed) != bool(cur.get("boq_scope_changed", False)):
        patch["boq_scope_changed"] = bool(boq_scope_changed)
        changes_desc.append(f"BOQ Changed → `{bool(boq_scope_changed)}`")
    if boq_scope_note is not None and boq_scope_note.strip() != (cur.get("boq_scope_note") or ""):
        patch["boq_scope_note"] = boq_scope_note.strip() or None
        changes_desc.append("BOQ Note updated")

    if not patch:
        return {
            "ok": True,
            "project_id": project_id,
            "order": cur,
            "receipt": f"No changes needed on `{cur['project_code']}`",
        }

    updated_rows = sb.table("projects").update(patch).eq("id", project_id).execute().data or [cur]
    updated = updated_rows[0]
    return {
        "ok": True,
        "project_id": project_id,
        "order": updated,
        "entity_undo": {
            "table": "projects",
            "record_id": project_id,
            "prior_snapshot": prior_snapshot,
        },
        "receipt": f"Order `{updated['project_code']}` (*{updated['customer_name']}*): {', '.join(changes_desc)}",
    }


async def archive_master_or_order_record(
    sb: Any,
    *,
    entity_kind: str,  # 'customer' | 'vendor' | 'person' | 'order'
    record_id: str,
    actor_person_id: str | None = None,
    source_channel: str = "web",
    note: str | None = None,
) -> dict[str, Any]:
    """Confirm-gated Soft Archive (never hard SQL DELETE)."""
    if entity_kind == "order":
        return await transition_project_pipeline(
            sb,
            project_id=record_id,
            to_stage="lost",
            actor_person_id=actor_person_id,
            source_channel=source_channel,
            note=note or "Archived / marked Lost",
        )
    if entity_kind == "customer":
        return await update_customer_record(sb, customer_id=record_id, is_active=False)
    if entity_kind == "vendor":
        return await update_vendor_record(sb, vendor_id=record_id, is_active=False)
    if entity_kind == "person":
        return update_person_record(sb, person_id=record_id, is_active=False)
    return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Unknown archive target '{entity_kind}'."}}


async def undo_entity_mutations(sb: Any, entity_undos: list[dict[str, Any]]) -> list[str]:
    """Revert one or more Master Spine / Order metadata snapshots in reverse order."""
    reverted: list[str] = []
    for item in reversed(entity_undos):
        table = item.get("table")
        rec_id = item.get("record_id")
        snap = item.get("prior_snapshot") or {}
        if not table or not rec_id or not snap:
            continue
        sb.table(table).update(snap).eq("id", rec_id).execute()
        for prj_undo in item.get("cascaded_projects_before") or []:
            if prj_undo.get("id") and prj_undo.get("patch"):
                sb.table("projects").update(prj_undo["patch"]).eq("id", prj_undo["id"]).execute()
        z_cid = item.get("zoho_contact_id")
        if z_cid and table in ("customers", "vendors"):
            await zoho.sync_existing_zoho_contact(
                z_cid,
                name=snap.get("full_name") or snap.get("canonical_name"),
                phone=snap.get("phone") or "",
                gstin=snap.get("gstin") if table == "vendors" else None,
                replace_notes=snap.get("notes") or "",
            )
        reverted.append(f"Reverted {table[:-1] if table.endswith('s') else table} update")
    return reverted
