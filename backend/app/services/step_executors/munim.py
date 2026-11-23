"""Munim -> Zoho Books Standard Plan step executor + Payment Track Smart Auto-Bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from app.services import goal_intelligence, zoho
from app.services.goal_primitives import (
    PAYMENT_TRACK_STAGES,
    format_inr,
    get_expense_category_label,
    make_zoho_reference_number,
)
from app.services.step_executors.common import sanitize_iso_date


async def execute_munim_financial_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    entry_kind: str,  # 'customer_receipt' | 'customer_payment' | 'vendor_payment' | 'expense' | 'float_transfer'
    amount_inr: float,
    payment_account_id: str,
    to_payment_account_id: str | None = None,
    party_name: str = "",
    project_id: str | None = None,
    zoho_contact_id: str | None = None,
    txn_date: str | None = None,
    notes: str = "",
    actor_label: str = "Munim",
    expense_category: str | None = None,
    is_explicit_advance: bool = False,
) -> dict[str, Any]:
    """Idempotent Munim -> Zoho Books Standard push + Smart Auto-Bridge Payment Track sync."""
    ref_no = make_zoho_reference_number(goal_id, step_index)
    clean_txn_date = sanitize_iso_date(txn_date)

    # 1. Push to Zoho Books (with reference_number dedup + Reporting Tag bridge)
    z_res = await zoho.push_management_entry_to_zoho(
        sb,
        entry_kind=entry_kind,
        amount_inr=amount_inr,
        payment_account_id=payment_account_id,
        to_payment_account_id=to_payment_account_id,
        party_name=party_name,
        zoho_contact_id=zoho_contact_id,
        project_id=project_id,
        reference_number=ref_no,
        txn_date=clean_txn_date,
        notes=notes,
        actor_label=actor_label,
        expense_category=expense_category,
    )
    if not z_res.get("ok"):
        return z_res

    # 2. Smart Auto-Bridge: Inflow matching an active Production order updates Payment Track
    pay_stage_note = ""
    project_undo_snapshot: dict[str, Any] | None = None
    if entry_kind in ("customer_receipt", "customer_payment") and project_id:
        p_rows = (
            sb.table("projects")
            .select("id,quoted_total,paid_cache_inr,payment_stage,pipeline_stage,pipeline_stage_at")
            .eq("id", project_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if p_rows:
            p = p_rows[0]
            prev_paid = float(p.get("paid_cache_inr") or 0)
            new_paid = round(prev_paid + float(amount_inr), 2)
            quoted = float(p.get("quoted_total") or 0)
            if quoted > 0 and new_paid >= quoted:
                new_pstage = "payment_complete"
            elif p.get("payment_stage") in (None, "", "advance_pending") or (
                p.get("payment_stage") == "advance_in" and prev_paid <= 0
            ):
                new_pstage = "advance_in"
            else:
                new_pstage = "partial_paid" if new_paid > 0 else p.get("payment_stage")

            prj_patch: dict[str, Any] = {}
            if new_pstage in PAYMENT_TRACK_STAGES:
                prj_patch["paid_cache_inr"] = new_paid
                prj_patch["payment_stage"] = new_pstage
                pay_stage_note = f" · Order Payment → *{new_pstage.replace('_', ' ').title()}*"

            if is_explicit_advance and p.get("pipeline_stage") in (
                "lead",
                "design",
                "estimating",
                "quoted_awaiting_advance",
            ):
                prj_patch["pipeline_stage"] = "site_sizing_boq"
                prj_patch["pipeline_stage_at"] = datetime.now(timezone.utc).isoformat()
                pay_stage_note += " · Pipeline → *Site Sizing / BOQ*"

            if prj_patch:
                project_undo_snapshot = {
                    "project_id": project_id,
                    "before": {k: p.get(k) for k in prj_patch},
                }
                sb.table("projects").update(prj_patch).eq("id", project_id).execute()
    elif entry_kind in ("expense", "vendor_payment") and project_id and float(amount_inr or 0) > 0:
        p_rows = (
            sb.table("projects")
            .select("id,cost_cache_inr")
            .eq("id", project_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if p_rows:
            old_cost = float(p_rows[0].get("cost_cache_inr") or 0)
            new_cost = round(old_cost + float(amount_inr), 2)
            project_undo_snapshot = {
                "project_id": project_id,
                "before": {"cost_cache_inr": old_cost},
            }
            sb.table("projects").update({"cost_cache_inr": new_cost}).eq("id", project_id).execute()
            pay_stage_note = f" · Order Cost Updated (+{format_inr(amount_inr)})"

    acc_row = sb.table("payment_accounts").select("display_name").eq("id", payment_account_id).limit(1).execute().data
    acc_label = acc_row[0]["display_name"] if acc_row else "Account"
    arrow = (
        "🟢 IN"
        if entry_kind in ("customer_receipt", "customer_payment")
        else ("🔄 TRANSFER" if entry_kind == "float_transfer" else "🔴 OUT")
    )
    z_badge = "Zoho Stub" if z_res.get("stubbed") else f"Zoho `{z_res.get('zoho_type')}`"
    cat_note = ""
    if entry_kind == "expense":
        cat_lbl = z_res.get("expense_account_name") or get_expense_category_label(expense_category)
        cat_note = f" · _{cat_lbl}_"
    elif entry_kind == "customer_receipt":
        cat_note = f" · _{z_res.get('income_account_name') or 'Sales'}_"

    return {
        "ok": True,
        "status_code": 200,
        "error_kind": "none",
        "zoho_id": z_res.get("zoho_id"),
        "zoho_type": z_res.get("zoho_type"),
        "reference_number": ref_no,
        **({"project_undo_snapshot": project_undo_snapshot} if project_undo_snapshot else {}),
        "receipt": f"{arrow} *{format_inr(amount_inr)}* ({acc_label}) — {party_name or entry_kind}{cat_note} [{z_badge}]{pay_stage_note}",
    }


async def execute_munim_draft_doc_step(
    sb: Any,
    *,
    goal_id: str,
    step_index: int,
    op: str,
    slots: dict[str, Any],
) -> dict[str, Any]:
    """Create strictly DRAFT-only AP Supplier Bills or AR Customer Invoices in Zoho Books India."""
    party_name = str(slots.get("party_name") or slots.get("full_name") or "").strip()
    amt = float(slots.get("amount_inr") or 0)
    line_items: list[dict[str, Any]] = []
    if slots.get("bill_line_items_json"):
        try:
            line_items = json.loads(str(slots["bill_line_items_json"]))
        except json.JSONDecodeError:
            line_items = []
    default_desc = "Workshop Material" if op == "supplier_draft_bill" else "Custom Furniture / Interior Work"
    if not line_items:
        line_items = [{"description": slots.get("notes") or default_desc, "quantity": 1, "rate": amt, "amount": amt}]
    ref_no = make_zoho_reference_number(goal_id, step_index)
    clean_date = sanitize_iso_date(slots.get("txn_date"))

    if op == "customer_draft_invoice":
        draft_res = await zoho.create_draft_invoice(
            customer_name=party_name or "Customer",
            invoice_date=clean_date,
            line_items=line_items,
            notes=str(slots.get("notes") or ""),
            customer_zoho_id=slots.get("zoho_contact_id"),
            reference_number=ref_no,
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
        draft_status = str(draft_res.get("status") or (draft_res.get("invoice") or {}).get("status") or "draft")
        inv_id = str((draft_res.get("invoice") or {}).get("invoice_id") or "")
        return {
            "ok": True,
            "draft_status": draft_status,
            "zoho_id": inv_id or None,
            "zoho_type": "invoice",
            "reference_number": ref_no,
            "receipt": f"📄 Zoho *Draft Invoice (AR)* saved: *{party_name}* · *{format_inr(amt)}*",
        }

    # supplier_draft_bill
    inv_no = slots.get("invoice_no")
    dup = await zoho.check_duplicate_bill(sb, party_name, inv_no, amt)
    if dup and not slots.get("force_ignore_duplicate"):
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {
                "code": "DUPLICATE_BILL",
                "message": f"Bill `{inv_no}` for `{party_name}` ({format_inr(amt)}) already exists in {dup['source']}.",
            },
        }
    payee = await zoho.resolve_payee_zoho_vendor(sb, party_name, allow_adhoc_create=True)
    if payee.get("error") and not payee.get("contact_id") and goal_intelligence.is_transient_zoho_error_message(
        str(payee.get("error") or "")
    ):
        return {"ok": False, "error_kind": "transient_5xx", "error": {"message": payee.get("error")}}
    draft_res = await zoho.create_draft_bill(
        supplier_name=payee.get("canonical_name") or party_name,
        invoice_no=inv_no,
        bill_date=clean_date,
        line_items=line_items,
        notes=str(slots.get("notes") or ""),
        vendor_id=payee.get("contact_id"),
        reference_number=ref_no,
        project_id=slots.get("project_id"),
        expense_category=slots.get("expense_category"),
        account_name=slots.get("account_name"),
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
    bill_obj = draft_res.get("bill") or {}
    bill_status = str(draft_res.get("status") or bill_obj.get("status") or "open")
    bill_id = str(bill_obj.get("bill_id") or "")
    bill_undo_snap: dict[str, Any] | None = None
    if slots.get("project_id") and amt > 0:
        pid = str(slots["project_id"])
        p_rows = (
            sb.table("projects")
            .select("id,cost_cache_inr,zoho_bills_cache")
            .eq("id", pid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if p_rows:
            old_cost = float(p_rows[0].get("cost_cache_inr") or 0)
            old_bills = float(p_rows[0].get("zoho_bills_cache") or 0)
            bill_undo_snap = {
                "project_id": pid,
                "before": {"cost_cache_inr": old_cost, "zoho_bills_cache": old_bills},
            }
            sb.table("projects").update(
                {"cost_cache_inr": round(old_cost + amt, 2), "zoho_bills_cache": round(old_bills + amt, 2)}
            ).eq("id", pid).execute()
    acc_note = f" · _{draft_res['expense_account_name']}_" if draft_res.get("expense_account_name") else ""
    return {
        "ok": True,
        "draft_status": bill_status,
        "zoho_id": bill_id or None,
        "zoho_type": "bill",
        "reference_number": ref_no,
        "expense_account_name": draft_res.get("expense_account_name"),
        **({"project_undo_snapshot": bill_undo_snap} if bill_undo_snap else {}),
        "receipt": f"🧾 Zoho *Open Bill (AP)* saved: *{party_name}* (`{inv_no or 'AP'}`) · *{format_inr(amt)}*{acc_note}",
    }
