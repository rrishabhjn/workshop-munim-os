"""Munim -> Zoho Books Standard Plan step executor + Payment Track Smart Auto-Bridge."""

from __future__ import annotations

from typing import Any

from app.services import zoho
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
    cat_note = ""
    if entry_kind == "expense":
        cat_lbl = z_res.get("expense_account_name") or get_expense_category_label(expense_category)
        cat_note = f" · _{cat_lbl}_"

    return {
        "ok": True,
        "status_code": 200,
        "error_kind": "none",
        "zoho_id": z_res.get("zoho_id"),
        "reference_number": ref_no,
        "receipt": f"{arrow} *{format_inr(amount_inr)}* ({acc_label}) — {party_name or entry_kind}{cat_note} [{z_badge}]{pay_stage_note}",
    }
