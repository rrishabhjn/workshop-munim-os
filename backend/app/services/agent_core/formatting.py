"""Prompt summaries, Before->After confirmation cards, step compilation, and goal persistence."""

from __future__ import annotations

from typing import Any

from app.services import goal_intelligence
from app.services.agent_core.slot_resolvers import parse_initial_items_slot
from app.services.goal_primitives import (
    CheckpointedStep,
    MATERIAL_TRACK_STAGES,
    PROJECT_PIPELINE_STAGES,
    format_inr,
    get_expense_category_label,
)
from app.services.step_executors.common import normalize_stage_code_for_op


def _default_missing_question(op: str, missing: list[str]) -> str:
    labels = {
        "full_name": "Full Name",
        "area_landmark": "Site Area / Landmark (e.g. Civil Lines, Jaipur)",
        "rough_scope": "Rough Scope (items/rooms)",
        "payment_account_code": "Payment Account (Cash Galla / Axis / Float)",
        "to_payment_account_code": "Destination Account / Float",
        "amount_inr": "Amount (₹)",
        "party_name": "Customer / Vendor Name",
        "customer_id": "Which Customer",
        "vendor_id": "Which Vendor",
        "person_id": "Which Person / Mistri",
        "project_id": "Which Customer Order",
        "archive_entity_kind": "Record Type (Customer / Vendor / Person / Order)",
        "item_name": "Furniture Item Name (e.g. Kitchen, Bed)",
        "material_lines_json": "Raw Material Name & Quantity (e.g. 8 sheets 18mm Ply)",
        "to_stage": "Target Stage",
    }
    nice = ", ".join(labels.get(m, m) for m in missing)
    return f"Kripya yeh details batayein ({op.replace('_', ' ')}): *{nice}*"


def _format_confirmation_summary(
    op: str, slots: dict[str, Any], before_row: dict[str, Any] | None = None
) -> str:
    row = before_row or {}
    if op == "new_lead":
        q_str = f" · Est: {format_inr(slots['quoted_total'])}" if slots.get("quoted_total") else ""
        items = parse_initial_items_slot(slots)
        items_str = f"\n• Items: *{len(items)} furniture item(s)* parsed" if items else ""
        stage_norm = (
            normalize_stage_code_for_op("pipeline_stage_move", slots.get("to_stage"))
            if slots.get("to_stage")
            else None
        )
        stage_str = (
            f"\n• Start Stage: *{stage_norm.replace('_', ' ').title()}*"
            if stage_norm and stage_norm in PROJECT_PIPELINE_STAGES and stage_norm != "lead"
            else ""
        )
        return (
            f"🆕 *Confirm New Lead*\n"
            f"• Customer: *{slots.get('full_name')}* ({slots.get('area_landmark')})\n"
            f"• Scope: _{slots.get('rough_scope')}_{q_str}{items_str}{stage_str}"
        )
    if op == "add_customer":
        cname = slots.get("full_name") or slots.get("party_name")
        area = f" ({slots['area_landmark']})" if slots.get("area_landmark") else ""
        ph = f"\n• Phone: `{slots['phone']}`" if slots.get("phone") else ""
        return f"👤 *Confirm New Customer*\n• Name: *{cname}*{area}{ph}"
    if op == "add_person_or_vendor":
        name = slots.get("full_name") or slots.get("party_name")
        kind = slots.get("person_or_vendor_kind") or "salaried_karigar"
        ph = f"\n• Phone: `{slots['phone']}`" if slots.get("phone") else ""
        gst = f"\n• GSTIN: `{slots['gstin']}`" if slots.get("gstin") else ""
        spec = f"\n• Specialty: _{slots['specialty']}_" if slots.get("specialty") else ""
        return f"🆕 *Confirm Directory Onboarding*\n• Name: *{name}* (`{kind}`){ph}{gst}{spec}"
    if op == "archive_record":
        kind = (slots.get("archive_entity_kind") or "record").title()
        label = (
            row.get("full_name")
            or row.get("canonical_name")
            or row.get("customer_name")
            or slots.get("full_name")
            or slots.get("party_name")
            or kind
        )
        code = f" (`{row['project_code']}`)" if row.get("project_code") else ""
        action_desc = "Mark Order *Lost / Closed*" if kind.lower() == "order" else "Soft-Archive (`is_active = false`, preserves history)"
        return f"🗂️ *Confirm Archive {kind}*\n• Target: *{label}*{code}\n• Action: {action_desc}"
    if op in ("update_customer", "update_vendor", "update_person", "update_order_details"):
        label = (
            row.get("full_name")
            or row.get("canonical_name")
            or row.get("customer_name")
            or slots.get("summary_title")
            or "Record"
        )
        diffs: list[str] = []
        old_name = row.get("full_name") or row.get("canonical_name") or ""
        if slots.get("new_name") and str(slots["new_name"]).strip().lower() != str(old_name).strip().lower():
            diffs.append(f"• Name: _{old_name or '-'}_ → *{slots['new_name']}*")
        if op == "update_order_details" and slots.get("quoted_total") is not None:
            old_q = format_inr(float(row.get("quoted_total") or 0))
            new_q = format_inr(float(slots["quoted_total"]))
            diffs.append(f"• Deal Value: {old_q} → *{new_q}*")
        if slots.get("gstin"):
            diffs.append(f"• GSTIN: `{row.get('gstin') or '-'}` → *`{str(slots['gstin']).upper()}`*")
        if slots.get("payout_aliases"):
            old_pa = ", ".join(row.get("payout_aliases") or []) or "None"
            diffs.append(f"• Bank Payout Proxy: _{old_pa}_ → + *{', '.join(slots['payout_aliases'])}*")
        if slots.get("phone"):
            diffs.append(f"• Phone: `{row.get('phone') or '-'}` → `{slots['phone']}`")
        if slots.get("area_landmark"):
            old_a = row.get("area_landmark") or row.get("site_area_landmark") or "-"
            diffs.append(f"• Area: _{old_a}_ → *{slots['area_landmark']}*")
        if slots.get("promised_delivery_date"):
            diffs.append(f"• Delivery: `{row.get('promised_delivery_date') or '-'}` → *`{slots['promised_delivery_date']}`*")
        if slots.get("rough_scope"):
            diffs.append(f"• Scope: _{slots['rough_scope']}_")
        diff_body = "\n".join(diffs) if diffs else f"• {slots.get('summary_title') or op}"
        return f"✏️ *Confirm Update — {label}*\n{diff_body}"
    if op in ("customer_receipt", "customer_payment", "vendor_payment", "expense", "float_transfer"):
        amt = format_inr(float(slots.get("amount_inr") or 0))
        acc = (slots.get("payment_account_code") or "").upper()
        is_inflow = op in ("customer_receipt", "customer_payment")
        badge = "🟢 PAISA AAYA (IN)" if is_inflow else ("🔄 TRANSFER" if op == "float_transfer" else "🔴 PAISA GAYA (OUT)")
        party = slots.get("party_name") or slots.get("to_payment_account_code") or op
        match_tag = ""
        if op != "float_transfer" and slots.get("party_name"):
            if slots.get("is_new_payee") is True:
                match_tag = " _(🆕 New Customer)_" if is_inflow else " _(🆕 New Vendor)_"
            elif slots.get("is_new_payee") is False or any(slots.get(k) for k in ("zoho_contact_id", "vendor_id", "person_id")):
                match_tag = " _(✅ Matched)_"
        cat_line = ""
        if op == "expense":
            cat_line = f"\n• Category: *{get_expense_category_label(slots.get('expense_category'))}*"
        elif op == "customer_receipt":
            cat_line = "\n• Type: *Sales / Revenue (Zoho Sales)*"
        elif op == "customer_payment":
            cat_line = "\n• Type: *AR Settlement (Against Invoice)*"
        elif op == "vendor_payment":
            cat_line = "\n• Type: *AP Settlement (Against Bill)*"
        return f"{badge}: *{amt}*\n• Party/To: *{party}*{match_tag}{cat_line}\n• Account: `{acc}`\n• Note: _{slots.get('notes') or slots.get('summary_title') or '-'}_"
    if op == "supplier_draft_bill":
        amt = format_inr(float(slots.get("amount_inr") or 0))
        match_tag = " _(🆕 New Vendor)_" if slots.get("is_new_payee") is True else (" _(✅ Matched)_" if slots.get("is_new_payee") is False or slots.get("zoho_contact_id") or slots.get("vendor_id") else "")
        return (
            f"🧾 *Confirm Zoho Draft Bill (Payable)*\n"
            f"• Vendor: *{slots.get('party_name')}*{match_tag}\n"
            f"• Invoice: `{slots.get('invoice_no') or 'Auto'}` · Total: *{amt}*\n"
            f"_(Strictly saved as Draft in Zoho Books for Clerk review)_"
        )
    if op == "customer_draft_invoice":
        amt = format_inr(float(slots.get("amount_inr") or 0))
        match_tag = " _(🆕 New Customer)_" if slots.get("is_new_payee") is True else (" _(✅ Matched)_" if slots.get("is_new_payee") is False or slots.get("zoho_contact_id") or slots.get("customer_id") else "")
        return (
            f"📄 *Confirm Zoho Draft Invoice (Receivable)*\n"
            f"• Customer: *{slots.get('party_name')}*{match_tag}\n"
            f"• Amount Due: *{amt}*\n"
            f"• Note: _{slots.get('notes') or slots.get('summary_title') or '-'}_\n"
            f"_(Strictly saved as Draft in Zoho Books for Clerk review)_"
        )
    return f"📝 *Confirm*: {slots.get('summary_title') or op}"


def _build_steps_for_slots(op: str, slots: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        CheckpointedStep(
            step_index=0,
            target="zoho" if op in ("customer_receipt", "customer_payment", "vendor_payment", "expense", "float_transfer", "supplier_draft_bill", "customer_draft_invoice") else "supabase",
            operation=op,
            payload=slots,
        ).model_dump()
    ]
    if op in ("new_lead", "update_order_details"):
        for it in parse_initial_items_slot(slots):
            steps.append(
                CheckpointedStep(
                    step_index=len(steps),
                    target="supabase",
                    operation="item_upsert_or_move",
                    payload={
                        "item_name": it["item_name"],
                        "dimensions": it.get("dimensions"),
                        "specs_notes": it.get("specs_notes"),
                        "to_stage": "in_shop",
                    },
                ).model_dump()
            )
    if op == "new_lead":
        lead_stage = (
            normalize_stage_code_for_op("pipeline_stage_move", slots.get("to_stage"))
            if slots.get("to_stage")
            else None
        )
        if lead_stage and lead_stage in PROJECT_PIPELINE_STAGES and lead_stage != "lead":
            steps.append(
                CheckpointedStep(
                    step_index=len(steps),
                    target="supabase",
                    operation="pipeline_stage_move",
                    payload={"to_stage": lead_stage},
                ).model_dump()
            )
        mat_stage = (
            normalize_stage_code_for_op("material_stage_move", slots.get("material_stage"))
            if slots.get("material_stage")
            else None
        )
        if (
            mat_stage
            and mat_stage in MATERIAL_TRACK_STAGES
            and mat_stage != "not_started"
            and not (lead_stage == "execution" and mat_stage == "check_stock")
        ):
            steps.append(
                CheckpointedStep(
                    step_index=len(steps),
                    target="supabase",
                    operation="material_stage_move",
                    payload={"to_stage": mat_stage},
                ).model_dump()
            )
    return steps


def _upsert_goal_row(
    sb: Any,
    *,
    goal_id: str | None,
    owner_person_id: str | None,
    source_channel: str,
    domain_scope: str,
    telegram_chat_id: int | None,
    ui_context: dict[str, Any],
    goal_type: str,
    status: str,
    thinking_level: str,
    heard_transcript: str | None,
    summary_title: str,
    collected_slots: dict[str, Any],
    missing_slots: list[str],
    pending_prompt: str | None,
    steps: list[dict[str, Any]],
    repair_options: list[dict[str, Any]],
    project_id: str | None,
    customer_id: str | None,
    user_message: str | None = None,
) -> dict[str, Any]:
    safe_owner = goal_intelligence._safe_uuid_for_db(sb, owner_person_id)
    safe_proj = goal_intelligence._safe_uuid_for_db(sb, project_id, project_lookup=True)
    safe_cust = goal_intelligence._safe_uuid_for_db(sb, customer_id)
    payload: dict[str, Any] = {
        "owner_person_id": safe_owner,
        "source_channel": source_channel,
        "domain_scope": domain_scope,
        "telegram_chat_id": telegram_chat_id,
        "ui_context": ui_context,
        "goal_type": goal_type,
        "status": status,
        "thinking_level_used": thinking_level,
        "heard_transcript": heard_transcript,
        "summary_title": summary_title[:180],
        "collected_slots": collected_slots,
        "missing_slots": missing_slots,
        "pending_prompt": pending_prompt,
        "steps": steps,
        "repair_options": repair_options,
        "project_id": safe_proj,
        "customer_id": safe_cust,
    }
    if user_message and user_message.strip():
        payload["turn_digest"] = [{"user": user_message.strip()[:400]}]
    if goal_id:
        res = sb.table("agent_goals").update(payload).eq("id", goal_id).execute().data
        return res[0]
    res = sb.table("agent_goals").insert(payload).execute().data
    return res[0]
