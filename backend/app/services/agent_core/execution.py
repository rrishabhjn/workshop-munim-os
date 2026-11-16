"""Checkpointed goal execution, 400 error self-healing, slot patching, edit, undo, park & discard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any

from app.services import goal_intelligence, step_executors, zoho
from app.services.agent_core.formatting import (
    _build_steps_for_slots,
    _default_missing_question,
    _format_confirmation_summary,
)
from app.services.agent_core.schemas import (
    ErrorRepairProposal,
    _inline_json_schema,
    _sanitize_gemini_nulls,
    _strip_json_fences,
)
from app.services.agent_core.slot_resolvers import (
    _backfill_financial_slots_from_text,
    _build_default_missing_slot_choices,
    _compute_missing_slots_for_op,
    _fetch_target_row_for_slots,
    _sanitize_and_resolve_slot_ids,
)
from app.services.goal_primitives import (
    claim_goal_for_execution,
    format_inr,
    is_high_sensitivity_master_mutation,
    make_zoho_reference_number,
)

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


async def confirm_and_execute_goal(
    sb: Any,
    *,
    goal_id: str,
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str = "web",
) -> dict[str, Any]:
    """Atomically claim and execute checkpointed steps; trigger Hydrogen repair on 400 errors."""
    pkg = _pkg()
    claimed = claim_goal_for_execution(sb, goal_id)
    if not claimed:
        existing = sb.table("agent_goals").select("id,status,summary_title").eq("id", goal_id).limit(1).execute().data
        st = existing[0]["status"] if existing else "unknown"
        return {
            "goal_id": goal_id,
            "status": st,
            "already_processed": True,
            "receipt": f"Already {st}.",
        }

    steps: list[dict[str, Any]] = list(claimed.get("steps") or [])
    slots: dict[str, Any] = _sanitize_gemini_nulls(dict(claimed.get("collected_slots") or {}))
    _sanitize_and_resolve_slot_ids(sb, slots)
    heard = claimed.get("heard_transcript")
    receipts: list[str] = []
    undo_ids: list[str] = list(claimed.get("undo_event_ids") or [])

    for idx, step in enumerate(steps):
        if step.get("status") == "succeeded":
            if step.get("result", {}).get("receipt"):
                receipts.append(step["result"]["receipt"])
            continue

        op = step.get("operation")
        res = await pkg._run_single_step(
            sb,
            goal_id=goal_id,
            step_index=idx,
            op=str(op),
            slots=slots,
            actor_person_id=actor_person_id,
            actor_label=actor_label,
            source_channel=source_channel,
            heard_transcript=heard,
        )

        if res.get("ok"):
            step["status"] = "succeeded"
            step["error_kind"] = "none"
            step["result"] = res
            receipts.append(res.get("receipt") or "Done")
            undo_ids.extend(res.get("undo_event_ids") or [])
            if res.get("project_id"):
                slots["project_id"] = res["project_id"]
            if res.get("customer_id"):
                slots["customer_id"] = res["customer_id"]
            continue

        # Step failed: distinguish Transient 5xx/429 vs Validation 400!
        err_kind = res.get("error_kind") or "validation_400"
        step["retry_count"] = int(step.get("retry_count") or 0) + 1
        step["error_kind"] = err_kind
        step["error_detail"] = res.get("error")

        if err_kind == "transient_5xx":
            attempt = int(step.get("retry_count") or 1)
            if attempt <= goal_intelligence.MAX_AUTO_ZOHO_RETRIES:
                step["status"] = "failed_transient"
                retry_info = goal_intelligence.enqueue_goal_retry_task(
                    goal_id=goal_id,
                    actor_person_id=actor_person_id,
                    actor_label=actor_label,
                    attempt=attempt,
                )
                meta_retry = {
                    **(claimed.get("metadata") or {}),
                    "auto_retry_attempt": attempt,
                    "auto_retry_queued": bool(retry_info.get("queued")),
                }
                pending_msg = (
                    f"⏳ Zoho/network busy — auto-retry {attempt}/"
                    f"{goal_intelligence.MAX_AUTO_ZOHO_RETRIES} scheduled (idempotency protected)."
                )
                sb.table("agent_goals").update(
                    {
                        "status": "awaiting_confirmation",
                        "steps": steps,
                        "metadata": meta_retry,
                        "pending_prompt": pending_msg,
                    }
                ).eq("id", goal_id).execute()
                return {
                    "goal_id": goal_id,
                    "status": "transient_retry_pending",
                    "auto_retry_attempt": attempt,
                    "auto_retry_queued": bool(retry_info.get("queued")),
                    "summary_title": claimed.get("summary_title"),
                    "prompt": (
                        f"⏳ Zoho momentarily busy — auto-retry {attempt}/"
                        f"{goal_intelligence.MAX_AUTO_ZOHO_RETRIES} scheduled in background (safe & idempotent)."
                    ),
                    "choices": [],
                }

            step["status"] = "failed_transient"
            exhausted_msg = (
                "⚠️ Zoho remained unreachable after 3 automatic retries — "
                "tap Confirm later or park for Clerk."
            )
            sb.table("agent_goals").update(
                {
                    "status": "needs_error_fix",
                    "steps": steps,
                    "pending_prompt": exhausted_msg,
                    "repair_options": [],
                }
            ).eq("id", goal_id).execute()
            return {
                "goal_id": goal_id,
                "status": "needs_error_fix",
                "summary_title": claimed.get("summary_title"),
                "prompt": exhausted_msg,
                "choices": [],
                "can_park_for_clerk": True,
            }

        # Validation 400 -> Invoke Hydrogen ('medium' thinking) Self-Healing!
        step["status"] = "failed_validation"
        diagnosis = await _diagnose_validation_error_with_hydrogen(
            summary_title=claimed.get("summary_title") or op,
            slots=slots,
            error_obj=res.get("error") or {},
        )
        repairs = [c.model_dump() for c in diagnosis.repair_options]
        sb.table("agent_goals").update(
            {
                "status": "needs_error_fix",
                "steps": steps,
                "pending_prompt": diagnosis.explanation_hinglish,
                "repair_options": repairs,
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "needs_error_fix",
            "summary_title": claimed.get("summary_title"),
            "prompt": diagnosis.explanation_hinglish,
            "choices": repairs,
            "can_park_for_clerk": True,
        }

    verification = goal_intelligence.verify_committed_goal_steps(
        sb, steps=steps, slots=slots
    )
    if not verification.get("verified"):
        msg = f"⚠️ Post-save verification caught a mismatch: {verification.get('mismatch')}"
        sb.table("agent_goals").update(
            {
                "status": "needs_error_fix",
                "steps": steps,
                "collected_slots": slots,
                "pending_prompt": msg,
                "repair_options": [],
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "needs_error_fix",
            "summary_title": claimed.get("summary_title"),
            "prompt": msg,
            "choices": [],
            "can_park_for_clerk": True,
            "verification": verification,
        }

    meta_done = {
        **(claimed.get("metadata") or {}),
        "verified_checks": verification.get("checks_run", 0),
    }
    sb.table("agent_goals").update(
        {
            "status": "completed",
            "steps": steps,
            "collected_slots": slots,
            "undo_event_ids": undo_ids,
            "project_id": goal_intelligence._safe_uuid_for_db(sb, slots.get("project_id"), project_lookup=True),
            "customer_id": goal_intelligence._safe_uuid_for_db(sb, slots.get("customer_id")),
            "metadata": meta_done,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", goal_id).execute()

    has_entity_undo = any(bool((s.get("result") or {}).get("entity_undo")) for s in steps)
    return {
        "goal_id": goal_id,
        "status": "completed",
        "summary_title": claimed.get("summary_title"),
        "receipt": "\n".join(receipts),
        "undo_available": len(undo_ids) > 0 or has_entity_undo,
        "undo_event_ids": undo_ids,
        "heard_transcript": heard,
        "verified": True,
        "project_id": slots.get("project_id"),
        "collected_slots": slots,
    }


async def _run_single_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    op: str,
    slots: dict[str, Any],
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str,
    heard_transcript: str | None,
) -> dict[str, Any]:
    src = "telegram" if source_channel == "telegram" else "web"
    if op == "pipeline_stage_move":
        return await step_executors.transition_project_pipeline(
            sb,
            project_id=str(slots["project_id"]),
            to_stage=str(slots["to_stage"]),
            actor_person_id=actor_person_id,
            estimator_ids=slots.get("estimator_ids") or None,
            sizing_mistri_id=slots.get("sizing_mistri_id"),
            boq_scope_changed=slots.get("boq_scope_changed"),
            source_channel=src,
            heard_transcript=heard_transcript,
            note=slots.get("notes"),
        )
    if op == "material_stage_move":
        return await step_executors.transition_material_track(
            sb,
            project_id=str(slots["project_id"]),
            to_stage=str(slots["to_stage"]),
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
            note=slots.get("notes"),
        )
    if op == "item_upsert_or_move":
        return await step_executors.upsert_or_transition_item(
            sb,
            project_id=str(slots["project_id"]),
            item_id=slots.get("item_id"),
            item_name=slots.get("item_name"),
            to_stage=str(slots.get("to_stage") or "in_shop"),
            production_mistri_id=slots.get("production_mistri_id"),
            assembly_mistri_id=slots.get("assembly_mistri_id"),
            dimensions=slots.get("dimensions"),
            specs_notes=slots.get("specs_notes"),
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
        )
    if op == "item_material_upsert":
        project_id = str(slots["project_id"])
        raw_lines_str = slots.get("material_lines_json")
        parsed_lines: list[dict[str, Any]] = []
        if raw_lines_str:
            try:
                loaded = json.loads(raw_lines_str) if isinstance(raw_lines_str, str) else raw_lines_str
                if isinstance(loaded, list):
                    parsed_lines = [dict(x) for x in loaded if isinstance(x, dict)]
                elif isinstance(loaded, dict):
                    parsed_lines = [dict(loaded)]
            except (TypeError, ValueError):
                parsed_lines = []

        target_status = slots.get("material_status")
        is_status_update_only = bool(target_status) and (
            not parsed_lines or all(x.get("quantity") in (None, "", 0) for x in parsed_lines)
        )
        if is_status_update_only:
            query = sb.table("item_materials").select("*").eq("project_id", project_id)
            item_id_filter = slots.get("item_id")
            if not item_id_filter and slots.get("item_name"):
                found_it = (
                    sb.table("project_items")
                    .select("id")
                    .eq("project_id", project_id)
                    .ilike("item_name", str(slots["item_name"]).strip())
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                if found_it:
                    item_id_filter = found_it[0]["id"]
            if item_id_filter:
                query = query.eq("item_id", item_id_filter)
            existing_mats = query.execute().data or []
            name_needles = [
                str(x.get("material_name") or "").strip().lower()
                for x in parsed_lines
                if str(x.get("material_name") or "").strip()
            ]
            if name_needles and existing_mats:
                existing_mats = [
                    m
                    for m in existing_mats
                    if any(n in str(m.get("material_name") or "").lower() for n in name_needles)
                ]
            if existing_mats:
                return step_executors.update_item_materials_rows(
                    sb,
                    material_ids=[str(m["id"]) for m in existing_mats],
                    patch={"status": str(target_status)},
                    actor_person_id=actor_person_id,
                    source_channel=src,
                    heard_transcript=heard_transcript,
                )

        if target_status:
            for line in parsed_lines:
                line.setdefault("status", str(target_status))

        return await step_executors.add_item_materials_batch(
            sb,
            project_id=project_id,
            item_id=slots.get("item_id"),
            item_name=slots.get("item_name"),
            materials=parsed_lines,
            actor_person_id=actor_person_id,
            source_channel=src,
            heard_transcript=heard_transcript,
        )
    if op == "new_lead":
        return step_executors.create_customer_and_lead(
            sb,
            full_name=str(slots.get("full_name") or ""),
            area_landmark=str(slots.get("area_landmark") or ""),
            rough_scope=str(slots.get("rough_scope") or ""),
            phone=slots.get("phone"),
            quoted_total=float(slots.get("quoted_total") or 0),
            promised_delivery_date=slots.get("promised_delivery_date"),
            existing_customer_id=slots.get("customer_id"),
            actor_person_id=actor_person_id,
        )
    if op == "add_customer":
        return step_executors.create_standalone_customer(
            sb,
            full_name=str(slots.get("full_name") or slots.get("party_name") or ""),
            area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_customer":
        return await step_executors.update_customer_record(
            sb,
            customer_id=str(slots["customer_id"]),
            full_name=slots.get("new_name"),
            area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_vendor":
        v_kind = slots.get("person_or_vendor_kind")
        return await step_executors.update_vendor_record(
            sb,
            vendor_id=str(slots["vendor_id"]),
            canonical_name=slots.get("new_name"),
            vendor_kind=v_kind if v_kind in ("supplier", "contractor") else None,
            phone=slots.get("phone"),
            gstin=slots.get("gstin"),
            specialty=slots.get("specialty"),
            aliases=slots.get("aliases") or None,
            notes=slots.get("notes"),
        )
    if op == "update_person":
        p_kind = slots.get("person_or_vendor_kind")
        return step_executors.update_person_record(
            sb,
            person_id=str(slots["person_id"]),
            full_name=slots.get("new_name"),
            person_type=p_kind if p_kind in ("operator", "salaried_karigar", "contractor_mistri") else None,
            role_tags=slots.get("role_tags") or None,
            phone=slots.get("phone"),
            aliases=slots.get("aliases") or None,
            payout_aliases=slots.get("payout_aliases") or None,
        )
    if op == "update_order_details":
        return step_executors.update_order_metadata(
            sb,
            project_id=str(slots["project_id"]),
            quoted_total=float(slots["quoted_total"]) if slots.get("quoted_total") is not None else None,
            promised_delivery_date=slots.get("promised_delivery_date"),
            rough_scope=slots.get("rough_scope"),
            site_area_landmark=slots.get("area_landmark"),
            phone=slots.get("phone"),
            boq_scope_changed=slots.get("boq_scope_changed"),
            boq_scope_note=slots.get("boq_scope_note") or slots.get("notes"),
        )
    if op == "archive_record":
        kind = str(slots.get("archive_entity_kind") or "")
        rec_id_map = {
            "customer": slots.get("customer_id"),
            "vendor": slots.get("vendor_id"),
            "person": slots.get("person_id"),
            "order": slots.get("project_id"),
        }
        rec_id = rec_id_map.get(kind)
        if not rec_id:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Missing ID for {kind} archive."}}
        return await step_executors.archive_master_or_order_record(
            sb,
            entity_kind=kind,
            record_id=str(rec_id),
            actor_person_id=actor_person_id,
            source_channel=src,
            note=slots.get("notes"),
        )
    if op in ("customer_receipt", "vendor_payment", "expense", "float_transfer"):
        return await step_executors.execute_munim_financial_step(
            sb,
            goal_id=goal_id,
            step_index=step_index,
            entry_kind=op,
            amount_inr=float(slots.get("amount_inr") or 0),
            payment_account_id=str(slots.get("payment_account_id") or ""),
            to_payment_account_id=slots.get("to_payment_account_id"),
            party_name=str(slots.get("party_name") or ""),
            project_id=slots.get("project_id"),
            zoho_contact_id=slots.get("zoho_contact_id"),
            txn_date=slots.get("txn_date"),
            notes=str(slots.get("notes") or ""),
            actor_label=actor_label,
            expense_category=slots.get("expense_category"),
        )
    if op == "supplier_draft_bill":
        vendor_name = str(slots.get("party_name") or "")
        amt = float(slots.get("amount_inr") or 0)
        inv_no = slots.get("invoice_no")
        dup = await zoho.check_duplicate_bill(sb, vendor_name, inv_no, amt)
        if dup and not slots.get("force_ignore_duplicate"):
            return {
                "ok": False,
                "error_kind": "validation_400",
                "error": {
                    "code": "DUPLICATE_BILL",
                    "message": f"Bill `{inv_no}` for `{vendor_name}` ({format_inr(amt)}) already exists in {dup['source']}.",
                },
            }
        line_items: list[dict[str, Any]] = []
        if slots.get("bill_line_items_json"):
            try:
                line_items = json.loads(str(slots["bill_line_items_json"]))
            except json.JSONDecodeError:
                line_items = []
        if not line_items:
            line_items = [{"description": slots.get("notes") or "Workshop Material", "quantity": 1, "rate": amt, "amount": amt}]
        c_res = await zoho.resolve_or_create_contact(sb, vendor_name, "vendor")
        if c_res.get("status") == "error" and goal_intelligence.is_transient_zoho_error_message(
            str(c_res.get("error") or "")
        ):
            return {
                "ok": False,
                "error_kind": "transient_5xx",
                "error": {"message": c_res.get("error")},
            }
        draft_res = await zoho.create_draft_bill(
            supplier_name=c_res.get("contact_name") or vendor_name,
            invoice_no=inv_no,
            bill_date=slots.get("txn_date"),
            line_items=line_items,
            notes=str(slots.get("notes") or ""),
            vendor_id=c_res.get("contact_id"),
            reference_number=make_zoho_reference_number(goal_id, step_index),
            project_id=slots.get("project_id"),
            sb=sb,
        )
        if draft_res.get("error"):
            err_msg = str(draft_res["error"])
            return {
                "ok": False,
                "error_kind": (
                    "transient_5xx"
                    if goal_intelligence.is_transient_zoho_error_message(err_msg)
                    else "validation_400"
                ),
                "error": {"message": err_msg},
            }
        draft_status = str(
            draft_res.get("status")
            or (draft_res.get("bill") or {}).get("status")
            or "draft"
        )
        return {
            "ok": True,
            "draft_status": draft_status,
            "receipt": f"🧾 Zoho *Draft Bill* saved: *{vendor_name}* (`{inv_no or 'Draft'}`) · *{format_inr(amt)}*",
        }
    if op == "add_person_or_vendor":
        kind = str(slots.get("person_or_vendor_kind") or "salaried_karigar")
        name = str(slots.get("full_name") or slots.get("party_name") or "").strip()
        if not name:
            return {"ok": False, "error_kind": "validation_400", "error": {"message": "Name is required."}}
        if kind in ("supplier", "contractor"):
            ins = sb.table("vendors").insert(
                {
                    "canonical_name": name,
                    "vendor_kind": kind,
                    "phone": slots.get("phone"),
                    "gstin": str(slots["gstin"]).strip().upper() if slots.get("gstin") else None,
                    "specialty": slots.get("specialty"),
                    "aliases": slots.get("aliases") or [],
                    "notes": slots.get("notes"),
                }
            ).execute().data
            return {"ok": True, "vendor_id": ins[0]["id"], "receipt": f"Added Vendor *{ins[0]['canonical_name']}* ({kind})"}
        ins = sb.table("people").insert(
            {
                "full_name": name,
                "person_type": "contractor_mistri" if kind == "contractor_mistri" else "salaried_karigar",
                "role_tags": slots.get("role_tags") or ["mistri"],
                "phone": slots.get("phone"),
                "aliases": slots.get("aliases") or [],
                "payout_aliases": slots.get("payout_aliases") or [],
            }
        ).execute().data
        return {"ok": True, "person_id": ins[0]["id"], "receipt": f"Added Mistri *{ins[0]['full_name']}* to Roster"}

    return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Unsupported step '{op}'."}}


async def _diagnose_validation_error_with_hydrogen(
    *, summary_title: str, slots: dict[str, Any], error_obj: dict[str, Any]
) -> ErrorRepairProposal:
    """Use gemini-4-hydrogen ('medium' thinking) to turn a 400 error into plain Hinglish + 1-tap repairs."""
    pkg = _pkg()
    settings = pkg.get_settings()
    if not settings.gemini_api_key:
        return ErrorRepairProposal(
            explanation_hinglish=f"⚠️ Save ruk gaya: {error_obj.get('message', error_obj)}",
            repair_options=[],
        )
    try:
        client = pkg._get_interactions_client()
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=settings.gemini_live_model,
            system_instruction=(
                "You diagnose Zoho Books India or Supabase data validation errors for a workshop owner. "
                "Explain clearly in 1-2 short Hinglish lines and propose 1-2 safe slot patches in repair_options."
            ),
            input=f"Goal: {summary_title}\nSlots: {json.dumps(slots, ensure_ascii=False)}\nError: {json.dumps(error_obj, ensure_ascii=False)}",
            generation_config={"thinking_level": "medium"},
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _inline_json_schema(
                    ErrorRepairProposal, force_required=("repair_options",)
                ),
            },
        )
        return ErrorRepairProposal.model_validate_json(_strip_json_fences(interaction.output_text or ""))
    except Exception as exc:
        logger.warning("Hydrogen error diagnosis fallback: %s", exc)
        return ErrorRepairProposal(
            explanation_hinglish=f"⚠️ Data check: {error_obj.get('message', str(error_obj))[:180]}",
            repair_options=[],
        )


async def apply_slot_patch_and_resume(
    sb: Any,
    *,
    goal_id: str,
    slot_patch: dict[str, Any],
    actor_person_id: str | None,
    actor_label: str,
    source_channel: str = "web",
    correction_note: str | None = None,
) -> dict[str, Any]:
    """Apply a 1-tap disambiguation or repair button patch and advance the goal."""
    pkg = _pkg()
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    prior_slots = dict(g.get("collected_slots") or {})
    merged_slots = _sanitize_gemini_nulls({**prior_slots, **slot_patch})
    _backfill_financial_slots_from_text(
        sb,
        merged_slots,
        user_text=correction_note,
        heard_transcript=g.get("heard_transcript"),
    )
    _sanitize_and_resolve_slot_ids(sb, merged_slots)
    await asyncio.to_thread(
        goal_intelligence.maybe_distill_procedural_memory,
        sb,
        prior_summary=str(g.get("summary_title") or ""),
        prior_slots=prior_slots,
        updated_slots=merged_slots,
        user_correction_text=correction_note,
    )
    op = str(merged_slots.get("operation_kind") or "stage_update")
    steps = _build_steps_for_slots(op, merged_slots)

    still_missing = _compute_missing_slots_for_op(op, merged_slots)
    if g.get("status") == "gathering_info" and still_missing:
        next_prompt = _default_missing_question(op, still_missing)
        next_choices = (
            _build_default_missing_slot_choices(sb, still_missing[0], op)
            if len(still_missing) == 1
            else []
        )
        sb.table("agent_goals").update(
            {
                "collected_slots": merged_slots,
                "missing_slots": still_missing,
                "pending_prompt": next_prompt,
                "repair_options": next_choices,
                "steps": steps,
                "status": "gathering_info",
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "gathering_info",
            "summary_title": g.get("summary_title"),
            "prompt": next_prompt,
            "choices": next_choices,
            "missing_slots": still_missing,
            "project_id": merged_slots.get("project_id"),
            "collected_slots": merged_slots,
        }

    target_before_row = _fetch_target_row_for_slots(sb, op, merged_slots)
    is_auto_commit = op in ("pipeline_stage_move", "material_stage_move", "item_upsert_or_move", "item_material_upsert") or (
        op in ("update_customer", "update_vendor", "update_person", "update_order_details")
        and not is_high_sensitivity_master_mutation(op, merged_slots, target_before_row)
    )
    if g.get("status") == "gathering_info" and not is_auto_commit:
        confirm_card_text = _format_confirmation_summary(op, merged_slots, target_before_row)
        sb.table("agent_goals").update(
            {
                "collected_slots": merged_slots,
                "missing_slots": [],
                "pending_prompt": confirm_card_text,
                "repair_options": [],
                "steps": steps,
                "status": "awaiting_confirmation",
            }
        ).eq("id", goal_id).execute()
        return {
            "goal_id": goal_id,
            "status": "awaiting_confirmation",
            "summary_title": g.get("summary_title"),
            "prompt": confirm_card_text,
            "choices": [],
            "project_id": merged_slots.get("project_id"),
            "collected_slots": merged_slots,
        }

    sb.table("agent_goals").update(
        {
            "collected_slots": merged_slots,
            "missing_slots": [],
            "steps": steps,
            "status": "awaiting_confirmation",
        }
    ).eq("id", goal_id).execute()

    return await pkg.confirm_and_execute_goal(
        sb,
        goal_id=goal_id,
        actor_person_id=actor_person_id,
        actor_label=actor_label,
        source_channel=source_channel,
    )


def edit_open_goal_slots(
    sb: Any,
    *,
    goal_id: str,
    slot_patch: dict[str, Any],
    correction_note: str | None = None,
) -> dict[str, Any]:
    """Apply an operator [✏️ Edit] change on an open goal, distill memory, and refresh card."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    prior_slots = dict(g.get("collected_slots") or {})
    merged_slots = _sanitize_gemini_nulls({**prior_slots, **slot_patch})
    _sanitize_and_resolve_slot_ids(sb, merged_slots)
    memory_res = goal_intelligence.maybe_distill_procedural_memory(
        sb,
        prior_summary=str(g.get("summary_title") or ""),
        prior_slots=prior_slots,
        updated_slots=merged_slots,
        user_correction_text=correction_note,
    )

    op = str(merged_slots.get("operation_kind") or g.get("goal_type") or "stage_update")
    steps = _build_steps_for_slots(op, merged_slots)
    target_before_row = _fetch_target_row_for_slots(sb, op, merged_slots)
    confirm_card_text = _format_confirmation_summary(op, merged_slots, target_before_row)

    sb.table("agent_goals").update(
        {
            "collected_slots": merged_slots,
            "missing_slots": [],
            "pending_prompt": confirm_card_text,
            "repair_options": [],
            "steps": steps,
            "status": "awaiting_confirmation",
        }
    ).eq("id", goal_id).execute()

    return {
        "ok": True,
        "goal_id": goal_id,
        "status": "awaiting_confirmation",
        "summary_title": g.get("summary_title"),
        "prompt": confirm_card_text,
        "choices": [],
        "project_id": merged_slots.get("project_id"),
        "collected_slots": merged_slots,
        "memory_distilled": memory_res,
    }


def undo_completed_goal(
    sb: Any, *, goal_id: str, actor_person_id: str | None = None
) -> dict[str, Any]:
    """1-tap [↩️ Undo] for auto-committed Production stage moves AND low-risk Master/Order edits."""
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    if g.get("status") == "undone":
        return {"ok": True, "already_undone": True, "reverted": []}
    event_ids = list(g.get("undo_event_ids") or [])
    reverted = step_executors.undo_stage_events(sb, event_ids, actor_person_id=actor_person_id)

    entity_undos = [
        (s.get("result") or {}).get("entity_undo")
        for s in (g.get("steps") or [])
        if (s.get("result") or {}).get("entity_undo")
    ]
    for item in reversed(entity_undos):
        table = item.get("table")
        rec_id = item.get("record_id")
        snap = item.get("prior_snapshot") or {}
        if table and rec_id and snap:
            sb.table(table).update(snap).eq("id", rec_id).execute()
            for prj_undo in item.get("cascaded_projects_before") or []:
                if prj_undo.get("id") and prj_undo.get("patch"):
                    sb.table("projects").update(prj_undo["patch"]).eq("id", prj_undo["id"]).execute()
            z_cid = item.get("zoho_contact_id")
            if z_cid and table in ("customers", "vendors"):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        zoho.sync_existing_zoho_contact(
                            z_cid,
                            name=snap.get("full_name") or snap.get("canonical_name"),
                            phone=snap.get("phone") or "",
                            gstin=snap.get("gstin") if table == "vendors" else None,
                            replace_notes=snap.get("notes") or "",
                        )
                    )
                except RuntimeError:
                    pass
            reverted.append(f"Reverted {table[:-1] if table.endswith('s') else table} update")

    sb.table("agent_goals").update({"status": "undone"}).eq("id", goal_id).execute()
    return {"ok": True, "reverted": reverted}


def park_goal_for_clerk(sb: Any, goal_id: str) -> dict[str, Any]:
    """1-tap [📥 Let Clerk Fix Later] -> routes goal to Needs Info queue."""
    sb.table("agent_goals").update({"status": "parked_for_clerk"}).eq("id", goal_id).execute()
    return {"ok": True, "goal_id": goal_id, "status": "parked_for_clerk"}


def discard_goal(sb: Any, goal_id: str) -> dict[str, Any]:
    """Explicit user discard (the ONLY way an unfinished goal closes besides completion)."""
    sb.table("agent_goals").update(
        {"status": "discarded", "resolved_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", goal_id).execute()
    return {"ok": True, "goal_id": goal_id, "status": "discarded"}
