"""Munim -> Zoho Books Standard Plan step executor + Payment Track Smart Auto-Bridge."""

from __future__ import annotations

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
    if entry_kind in ("customer_receipt", "customer_payment") and project_id:
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
        "reference_number": ref_no,
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
        return {
            "ok": True,
            "draft_status": draft_status,
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
    draft_status = str(draft_res.get("status") or (draft_res.get("bill") or {}).get("status") or "draft")
    return {
        "ok": True,
        "draft_status": draft_status,
        "receipt": f"🧾 Zoho *Draft Bill (AP)* saved: *{party_name}* (`{inv_no or 'Draft'}`) · *{format_inr(amt)}*",
    }
