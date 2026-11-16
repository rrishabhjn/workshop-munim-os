"""Production lifecycle, lead onboarding, undo, and queue enrichment step executors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import zoho
from app.services.goal_primitives import (
    IST,
    ITEM_EXECUTION_STAGES,
    MATERIAL_TRACK_STAGES,
    PROJECT_PIPELINE_STAGES,
    build_stage_undo_snapshot,
)
from app.services.step_executors.common import (
    _compute_duration_hours,
    _norm_phone10,
    _notify_operator_handoff,
    get_default_person_id,
    normalize_stage_code_for_op,
    sanitize_iso_date,
)


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
    to_stage = normalize_stage_code_for_op("pipeline_stage_move", to_stage) or to_stage
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
    to_stage = normalize_stage_code_for_op("material_stage_move", to_stage) or to_stage
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
    to_stage = normalize_stage_code_for_op("item_upsert_or_move", to_stage) or "in_shop"
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
            mat_act = snap.get("_item_material_action")
            if mat_act == "created":
                for mid in snap.get("material_ids") or []:
                    sb.table("item_materials").delete().eq("id", mid).execute()
                reverted.append("Removed added raw material(s)")
            elif mat_act == "updated":
                for b in snap.get("before_rows") or []:
                    mid = b.get("id")
                    if mid:
                        patch = {k: v for k, v in b.items() if k != "id"}
                        sb.table("item_materials").update(patch).eq("id", mid).execute()
                reverted.append("Reverted raw material update")
            elif mat_act == "deleted" and snap.get("deleted_row"):
                sb.table("item_materials").insert(snap["deleted_row"]).execute()
                reverted.append("Restored deleted raw material")
            elif snap.get("_created"):
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
    force_new_customer: bool = False,
    actor_person_id: str | None = None,
) -> dict[str, Any]:
    """Create a canonical Customer (or link repeat Customer) + Supabase Lead Project.
    Dead leads never pollute Zoho Books until conversion!
    """
    cust_id = existing_customer_id
    cname = full_name.strip()
    area = area_landmark.strip()

    if not cust_id and not force_new_customer and cname:
        candidates = (
            sb.table("customers")
            .select("id,full_name,area_landmark,phone,is_active")
            .ilike("full_name", cname)
            .limit(10)
            .execute()
            .data
            or []
        )
        exact = [
            c
            for c in candidates
            if c.get("is_active") is not False
            and str(c.get("full_name") or "").strip().lower() == cname.lower()
        ]
        in_phone = _norm_phone10(phone)
        if in_phone:
            exact = [
                c
                for c in exact
                if not _norm_phone10(c.get("phone")) or _norm_phone10(c.get("phone")) == in_phone
            ]
        if len(exact) > 1 and area:
            area_low = area.lower()
            area_matches = [
                c for c in exact if str(c.get("area_landmark") or "").strip().lower() == area_low
            ]
            if area_matches:
                exact = area_matches
        if len(exact) == 1:
            cust_id = str(exact[0]["id"])
            cname = str(exact[0].get("full_name") or cname)
            area = area or str(exact[0].get("area_landmark") or "")
            phone = phone or exact[0].get("phone")

    if not cust_id:
        ins_c = (
            sb.table("customers")
            .insert(
                {
                    "full_name": cname,
                    "area_landmark": area,
                    "phone": phone,
                    "is_active": True,
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
    clean_delivery = sanitize_iso_date(promised_delivery_date)
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
                "promised_delivery_date": clean_delivery,
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


def enrich_whos_doing_what_rows(
    sb: Any, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Backfill queue ownership for packed_ready items even before SQL migration 007 runs.

    Routes unassigned `packed_ready` items to the active `is_default_qc` owner (Shreyansh),
    falling back to `project_items.production_mistri_id` if no default QC owner exists.
    """
    if not rows:
        return []

    unassigned_packed_item_ids = [
        str(r["item_id"])
        for r in rows
        if r.get("work_kind") == "item_execution"
        and r.get("stage_code") == "packed_ready"
        and not (r.get("assigned_person_ids") or [])
        and r.get("item_id")
    ]
    if not unassigned_packed_item_ids:
        return rows

    default_qc_id = get_default_person_id(sb, "is_default_qc")
    mistri_by_item: dict[str, str] = {}
    if not default_qc_id:
        pi_rows = (
            sb.table("project_items")
            .select("id,production_mistri_id")
            .in_("id", unassigned_packed_item_ids)
            .execute()
            .data
            or []
        )
        for pi in pi_rows:
            if pi.get("production_mistri_id"):
                mistri_by_item[str(pi["id"])] = str(pi["production_mistri_id"])

    enriched: list[dict[str, Any]] = []
    for row in rows:
        if (
            row.get("work_kind") == "item_execution"
            and row.get("stage_code") == "packed_ready"
            and not (row.get("assigned_person_ids") or [])
        ):
            owner_id = default_qc_id or mistri_by_item.get(str(row.get("item_id") or ""))
            if owner_id:
                enriched.append({**row, "assigned_person_ids": [owner_id]})
                continue
        enriched.append(row)
    return enriched
