"""Zoho Books Standard Plan management ledger pushes, CoA routing, and bank account matching."""

from __future__ import annotations

from datetime import datetime
import re
import sys
from typing import Any

import httpx

from app.services.goal_primitives import (
    EXPENSE_CATEGORIES,
    IST,
    build_zoho_standard_order_tags,
    classify_external_error,
    get_expense_category_label,
    normalize_or_infer_expense_category,
)


def _pkg() -> Any:
    return sys.modules[__package__]


def _pick_matching_zoho_bank_account(
    local_acc: dict[str, Any],
    zoho_accs: list[dict[str, Any]],
    claimed_zoho_ids: set[str],
) -> dict[str, Any] | None:
    """Safely match a local payment_account to a live Zoho bank/cash account.

    Prevents word-overlap bugs ('RGB' matching 'CCRGB') and protects Cash Wallets
    (supervisor_float) from binding to firm bank accounts or 'Cash Galla'/'Petty Cash'.
    """
    code = str(local_acc.get("code") or "").strip().lower()
    kind = str(local_acc.get("account_kind") or "").strip().lower()

    eligible: list[dict[str, Any]] = []
    for ba in zoho_accs:
        zid = str(ba.get("account_id") or "").strip()
        aname = (ba.get("account_name") or "").strip().lower()
        atype = (ba.get("account_type") or "").strip().lower()
        if not zid or zid in claimed_zoho_ids or ba.get("is_active") is False:
            continue
        if aname == "undeposited funds":
            continue
        if kind == "supervisor_float":
            if aname in ("petty cash", "cash galla", "cash"):
                continue
            if atype and atype != "cash" and ("wallet" not in aname and "float" not in aname):
                continue
        eligible.append(ba)

    if code == "cash_galla":
        for preferred in ("cash galla", "cash", "petty cash"):
            hit = next(
                (z for z in eligible if (z.get("account_name") or "").strip().lower() == preferred),
                None,
            )
            if hit:
                return hit

    candidates_raw = [
        str(local_acc.get("display_name") or ""),
        code,
        code.replace("_", " "),
        *[str(a) for a in (local_acc.get("aliases") or [])],
    ]
    candidates = [
        c.strip().lower()
        for c in candidates_raw
        if c and c.strip() and c.strip().lower() not in ("cash", "nakad")
    ]

    for cand in candidates:
        for ba in eligible:
            aname = (ba.get("account_name") or "").strip().lower()
            if aname == cand:
                return ba

    for cand in candidates:
        pat = re.compile(rf"\b{re.escape(cand)}\b")
        for ba in eligible:
            aname = (ba.get("account_name") or "").strip().lower()
            if pat.search(aname):
                return ba
    return None


async def _post_zoho_with_order_tag_fallback(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    has_order_tags: bool,
) -> httpx.Response:
    """POST to Zoho Books and retry once without custom_fields/tags on field-config 400."""
    pkg = _pkg()
    resp = await pkg._zoho_request(client, "POST", path, json_body=payload)
    if not resp.is_success and resp.status_code == 400 and has_order_tags:
        err_low = resp.text.lower()
        if any(k in err_low for k in ("custom", "field", "tag")):
            fallback_payload = {
                k: v for k, v in payload.items() if k not in ("custom_fields", "tags")
            }
            if isinstance(fallback_payload.get("line_items"), list):
                fallback_payload["line_items"] = [
                    {lk: lv for lk, lv in li.items() if lk != "tags"}
                    if isinstance(li, dict)
                    else li
                    for li in fallback_payload["line_items"]
                ]
            resp = await pkg._zoho_request(client, "POST", path, json_body=fallback_payload)
    return resp


async def _resolve_zoho_paid_account_id(
    client: httpx.AsyncClient, sb: Any, payment_account_row: dict[str, Any]
) -> str | None:
    """Resolve Zoho Books account_id for one of the canonical payment_accounts."""
    if payment_account_row.get("zoho_account_id"):
        return str(payment_account_row["zoho_account_id"])

    pkg = _pkg()
    resp = await pkg._zoho_request(client, "GET", "/bankaccounts")
    if resp.is_success:
        b_accs = resp.json().get("bankaccounts") or []
        all_local = sb.table("payment_accounts").select("id,zoho_account_id").eq("is_active", True).execute().data or []
        claimed_ids = {
            str(r["zoho_account_id"])
            for r in all_local
            if r.get("zoho_account_id") and str(r.get("id")) != str(payment_account_row.get("id"))
        }
        match = _pick_matching_zoho_bank_account(payment_account_row, b_accs, claimed_ids)
        if match:
            zid = str(match["account_id"])
            sb.table("payment_accounts").update({"zoho_account_id": zid}).eq(
                "id", payment_account_row["id"]
            ).execute()
            return zid
    return None


def _pick_zoho_expense_account(
    accounts: list[dict[str, Any]],
    expense_category: str | None,
    *,
    account_name: str | None = None,
) -> dict[str, Any] | None:
    """Select best matching Zoho Books Expense / COGS account for an expense_category."""
    active_exp = [
        a
        for a in accounts
        if a.get("account_type") in ("expense", "cost_of_goods_sold", "other_expense")
        and a.get("is_active", True)
    ]
    if not active_exp:
        return None

    if account_name and account_name.strip():
        override_low = account_name.strip().lower()
        hit = next(
            (a for a in active_exp if (a.get("account_name") or "").strip().lower() == override_low),
            None,
        ) or next(
            (a for a in active_exp if override_low in (a.get("account_name") or "").strip().lower()),
            None,
        )
        if hit:
            return hit

    cat_code = normalize_or_infer_expense_category(expense_category)
    target_names = [
        n.lower()
        for n in (EXPENSE_CATEGORIES.get(cat_code) or EXPENSE_CATEGORIES["other_expense"])[
            "zoho_names"
        ]
    ]

    cogs_only = [a for a in active_exp if a.get("account_type") == "cost_of_goods_sold"]
    search_pools = (
        (cogs_only, active_exp)
        if cat_code in ("raw_materials", "contractor_labor") and cogs_only
        else (active_exp,)
    )

    for pool in search_pools:
        for tname in target_names:
            exact = next(
                (a for a in pool if (a.get("account_name") or "").strip().lower() == tname),
                None,
            )
            if exact:
                return exact

        for tname in target_names:
            sub = next(
                (a for a in pool if tname in (a.get("account_name") or "").strip().lower()),
                None,
            )
            if sub:
                return sub

    for fallback_name in ("other expenses", "uncategorized"):
        fb = next(
            (a for a in active_exp if fallback_name in (a.get("account_name") or "").strip().lower()),
            None,
        )
        if fb:
            return fb

    return active_exp[0]


_SYSTEM_OR_TEMPLATE_LIABILITY_NAMES: frozenset[str] = frozenset(
    {
        "mortgages", "construction loans", "tds payable", "unearned revenue",
        "opening balance adjustments", "employee reimbursements", "gst payable",
        "output igst", "output cgst", "output sgst", "tax payable",
        "dimension adjustments", "pre-cutover offset", "pre-cutover clearing",
    }
)


def pick_zoho_loan_liability_account(
    accounts: list[dict[str, Any]],
    *,
    account_name: str | None = None,
    hint_text: str = "",
    bank_hint: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select custom Zoho Loan Liability CoA account, or return ambiguous loan candidates."""
    all_liab = [
        a for a in (accounts or [])
        if a.get("account_type") in ("long_term_liability", "other_current_liability", "other_liability")
        and a.get("is_active", True) and not a.get("is_system_account")
    ]
    if not all_liab:
        return None, []
    if account_name and account_name.strip():
        ov = account_name.strip().lower()
        exact = next((a for a in all_liab if (a.get("account_name") or "").strip().lower() == ov), None)
        if exact:
            return exact, []
        subs = [a for a in all_liab if ov in (a.get("account_name") or "").strip().lower()]
        if subs:
            return (subs[0], []) if len(subs) == 1 else (None, subs)
    custom_loans = [a for a in all_liab if (a.get("account_name") or "").strip().lower() not in _SYSTEM_OR_TEMPLATE_LIABILITY_NAMES]
    if len(custom_loans) <= 1:
        return (custom_loans[0], []) if custom_loans else (None, [])
    stop = {"loan", "term", "emi", "principal", "paid", "repayment", "bank", "account", "muldhan", "mooldhan", "kisht"}
    for raw_h in (hint_text, bank_hint):
        toks = {t for t in re.findall(r"[a-z0-9]+", (raw_h or "").lower()) if len(t) >= 2} - stop
        if toks:
            hits = [a for a in custom_loans if toks & set(re.findall(r"[a-z0-9]+", (a.get("account_name") or "").lower()))]
            if len(hits) == 1:
                return hits[0], []
    return None, custom_loans


async def _push_loan_principal_to_zoho(
    client: httpx.AsyncClient,
    *,
    from_acc: dict[str, Any],
    from_zoho_acc: str,
    amount_inr: float,
    party_name: str,
    notes: str,
    desc: str,
    reference_number: str,
    txn_date: str | None,
    account_name: str | None,
) -> dict[str, Any]:
    """Post a Loan Principal repayment against a custom Zoho Liability account with safe fallbacks."""
    pkg = _pkg()
    for pth, lst_k, id_k, z_tp in (
        ("/expenses", "expenses", "expense_id", "expense"),
        ("/banktransactions", "banktransactions", "transaction_id", "banktransaction"),
        ("/journals", "journals", "journal_id", "journal"),
    ):
        ex_id = await pkg._find_existing_by_reference(client, pth, lst_k, id_k, reference_number)
        if ex_id:
            return {"ok": True, "status_code": 200, "error_kind": "none", "zoho_type": z_tp, "zoho_id": ex_id, "deduped": True, "expense_category": "loan_principal"}

    accounts = await pkg._get_chart_of_accounts(client)
    loan_coa, amb_loans = pick_zoho_loan_liability_account(
        accounts, account_name=account_name, hint_text=f"{party_name} {notes}", bank_hint=str(from_acc.get("display_name") or "")
    )
    if amb_loans:
        names_list = ", ".join(str(a.get("account_name") or "") for a in amb_loans)
        return {"ok": False, "status_code": 400, "error_kind": "validation_400", "error": {"message": f"Multiple Loan Liability accounts in Zoho ({names_list}) — specify account_name."}}
    if not loan_coa:
        return {"ok": False, "status_code": 400, "error_kind": "validation_400", "error": {"message": "No active Loan Liability account (e.g. RGB Term Loan) found in Zoho Chart of Accounts."}}

    def _ok_loan(resp_obj: httpx.Response, z_tp: str, obj_key: str, id_key: str) -> dict[str, Any]:
        return {
            "ok": True, "status_code": resp_obj.status_code, "error_kind": "none",
            "zoho_type": z_tp, "zoho_id": (resp_obj.json().get(obj_key) or {}).get(id_key),
            "expense_category": "loan_principal", "expense_account_name": loan_coa.get("account_name"),
        }

    amt_rounded = round(float(amount_inr), 2)
    iso_date = txn_date or datetime.now(IST).date().isoformat()
    loan_payload = {
        "account_id": loan_coa["account_id"], "paid_through_account_id": from_zoho_acc,
        "amount": amt_rounded, "reference_number": reference_number, "description": desc,
        "is_inclusive_tax": False, **({"date": txn_date} if txn_date else {}),
    }
    resp = await pkg._zoho_request(client, "POST", "/expenses", json_body=loan_payload)
    if resp.is_success:
        return _ok_loan(resp, "expense", "expense", "expense_id")
    if resp.status_code == 400:
        for bt_type in ("expense", "other_withdrawal", "transfer_fund"):
            bt_resp = await pkg._zoho_request(
                client, "POST", "/banktransactions",
                json_body={"transaction_type": bt_type, "from_account_id": from_zoho_acc, "to_account_id": loan_coa["account_id"], "amount": amt_rounded, "date": iso_date, "reference_number": reference_number, "description": desc},
            )
            if bt_resp.is_success:
                return _ok_loan(bt_resp, "banktransaction", "banktransaction", "transaction_id")
            if bt_resp.status_code != 400:
                resp = bt_resp
                break
        else:
            j_note = re.sub(r"[<>]+", " ", desc.replace("->", " to "))[:95].strip() or "Loan Principal"
            j_resp = await pkg._zoho_request(
                client, "POST", "/journals",
                json_body={
                    "journal_date": iso_date, "reference_number": reference_number, "notes": j_note,
                    "line_items": [
                        {"account_id": loan_coa["account_id"], "debit_or_credit": "debit", "amount": amt_rounded, "description": j_note},
                        {"account_id": from_zoho_acc, "debit_or_credit": "credit", "amount": amt_rounded, "description": j_note},
                    ],
                },
            )
            if j_resp.is_success:
                return _ok_loan(j_resp, "journal", "journal", "journal_id")
            resp = j_resp
    return {
        "ok": False, "status_code": resp.status_code, "error_kind": classify_external_error(resp.status_code),
        "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
    }


def _pick_zoho_income_account(accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select active Zoho Books Income account ('Sales' first, then 'General Income')."""
    active_inc = [
        a
        for a in accounts
        if a.get("account_type") in ("income", "other_income") and a.get("is_active", True)
    ]
    if not active_inc:
        return None
    for preferred in ("sales", "general income"):
        exact = next(
            (a for a in active_inc if (a.get("account_name") or "").strip().lower() == preferred),
            None,
        )
        if exact:
            return exact
    return active_inc[0]


async def push_management_entry_to_zoho(
    sb: Any,
    *,
    entry_kind: str,  # 'customer_receipt' | 'customer_payment' | 'vendor_payment' | 'expense' | 'float_transfer'
    amount_inr: float,
    payment_account_id: str,
    to_payment_account_id: str | None = None,
    party_name: str = "",
    zoho_contact_id: str | None = None,
    project_id: str | None = None,
    reference_number: str,
    txn_date: str | None = None,
    notes: str = "",
    actor_label: str = "Munim",
    expense_category: str | None = None,
    account_name: str | None = None,
) -> dict[str, Any]:
    """Push a confirmed management ledger transaction to Zoho Books India (GST-off Standard Plan)
    with reference_number idempotency and 1:1 Project Reporting Tag linking.
    """
    pkg = _pkg()
    settings = pkg.get_settings()
    acc_rows = (
        sb.table("payment_accounts")
        .select("*")
        .eq("id", payment_account_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not acc_rows:
        return {
            "ok": False,
            "status_code": 400,
            "error_kind": "validation_400",
            "error": {"message": "Payment account is required (e.g., Cash Galla, Axis, Float)."},
        }
    from_acc = acc_rows[0]

    bridge: dict[str, Any] = {}
    if project_id:
        bridge = await pkg.ensure_project_zoho_bridge(sb, project_id)
        if not zoho_contact_id and entry_kind in ("customer_receipt", "customer_payment"):
            zoho_contact_id = bridge.get("zoho_contact_id")

    if not settings.zoho_enabled:
        z_type = "sales_deposit" if entry_kind == "customer_receipt" else entry_kind
        stub_out: dict[str, Any] = {
            "ok": True,
            "stubbed": True,
            "status_code": 200,
            "error_kind": "none",
            "zoho_type": z_type,
            "zoho_id": f"STUB-{reference_number}",
            "account_name": from_acc["display_name"],
        }
        if entry_kind == "customer_receipt":
            stub_out["income_account_name"] = "Sales"
        if entry_kind == "expense":
            inferred_cat = normalize_or_infer_expense_category(expense_category, notes, party_name)
            stub_out["expense_category"] = inferred_cat
            stub_out["expense_account_name"] = (
                (account_name or "RGB Term Loan")
                if inferred_cat == "loan_principal"
                else (account_name or get_expense_category_label(inferred_cat))
            )
        return stub_out

    order_tags = build_zoho_standard_order_tags(
        project_code=bridge.get("cf_project_code"),
        zoho_tag_id=bridge.get("zoho_tag_id") or settings.zoho_project_tag_id,
        zoho_tag_option_id=bridge.get("zoho_tag_option_id"),
        custom_field_api_name=settings.zoho_project_custom_field,
    )
    proj_prefix = f"[{bridge['cf_project_code']}] " if bridge.get("cf_project_code") else ""
    desc = (
        f"[{actor_label}] {proj_prefix}{party_name} — {notes}"
        if (party_name and notes)
        else f"[{actor_label}] {proj_prefix}{party_name} {notes}"
    ).strip()[:250]

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            from_zoho_acc = await _resolve_zoho_paid_account_id(client, sb, from_acc)
            if not from_zoho_acc:
                return {
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {
                        "message": f"Account '{from_acc['display_name']}' is not yet mapped to a Zoho Books Bank/Cash/Petty-Cash account."
                    },
                }

            if entry_kind == "float_transfer":
                if not to_payment_account_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": "Destination account/float is required for a transfer."},
                    }
                to_rows = (
                    sb.table("payment_accounts")
                    .select("*")
                    .eq("id", to_payment_account_id)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )
                to_zoho_acc = (
                    await _resolve_zoho_paid_account_id(client, sb, to_rows[0])
                    if to_rows
                    else None
                )
                if not to_zoho_acc:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": "Destination Zoho account is not mapped yet."},
                    }
                existing_id = await pkg._find_existing_by_reference(
                    client, "/banktransactions", "banktransactions", "transaction_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "transfer_fund",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                payload = {
                    "transaction_type": "transfer_fund",
                    "from_account_id": from_zoho_acc,
                    "to_account_id": to_zoho_acc,
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": desc,
                    **({"date": txn_date} if txn_date else {}),
                }
                resp = await pkg._zoho_request(client, "POST", "/banktransactions", json_body=payload)
                if resp.is_success:
                    tid = (resp.json().get("banktransaction") or {}).get("transaction_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "transfer_fund",
                        "zoho_id": tid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            if entry_kind == "customer_receipt":
                existing_id = await pkg._find_existing_by_reference(
                    client, "/banktransactions", "banktransactions", "transaction_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "sales_deposit",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                # Routine Sales inflows (/banktransactions) do not create Zoho Customer Contacts
                # for walk-in / one-off buyers (party_name is already stamped in desc); attach
                # customer_id only when already cached on the linked customer/project.
                accounts = await pkg._get_chart_of_accounts(client)
                sales_coa = _pick_zoho_income_account(accounts)
                if not sales_coa:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": "No active Sales / Income account found in Zoho Books Chart of Accounts."},
                    }
                payment_mode = (
                    "Cash"
                    if from_acc["account_kind"] in ("cash_galla", "supervisor_float")
                    else "Bank Transfer"
                )
                resp = None
                for tx_type in ("deposit", "sales_without_invoices", "other_income"):
                    bt_payload: dict[str, Any] = {
                        "transaction_type": tx_type,
                        "from_account_id": sales_coa["account_id"],
                        "to_account_id": from_zoho_acc,
                        "amount": round(float(amount_inr), 2),
                        "date": txn_date or datetime.now(IST).date().isoformat(),
                        "payment_mode": payment_mode,
                        "reference_number": reference_number,
                        "description": desc,
                        **({"customer_id": zoho_contact_id} if zoho_contact_id else {}),
                        **order_tags,
                    }
                    resp = await _post_zoho_with_order_tag_fallback(
                        client, "/banktransactions", bt_payload, has_order_tags=bool(order_tags)
                    )
                    if not resp.is_success and resp.status_code == 400 and zoho_contact_id and "customer" in resp.text.lower():
                        no_cust = {k: v for k, v in bt_payload.items() if k != "customer_id"}
                        resp = await _post_zoho_with_order_tag_fallback(
                            client, "/banktransactions", no_cust, has_order_tags=bool(order_tags)
                        )
                    if resp.is_success or resp.status_code != 400:
                        break
                if resp is not None and resp.is_success:
                    rj = resp.json()
                    bt_obj = rj.get("banktransaction") or {}
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "sales_deposit",
                        "zoho_id": bt_obj.get("transaction_id"),
                        "income_account_name": sales_coa.get("account_name") or "Sales",
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code if resp is not None else 400,
                    "error_kind": classify_external_error(resp.status_code) if resp is not None else "validation_400",
                    "error": (
                        resp.json()
                        if resp is not None and "json" in resp.headers.get("content-type", "")
                        else {"message": resp.text[:240] if resp is not None else "Bank transaction failed"}
                    ),
                }

            if entry_kind == "customer_payment":
                existing_id = await pkg._find_existing_by_reference(
                    client, "/customerpayments", "customerpayments", "payment_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "customer_payment",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                if not zoho_contact_id and party_name:
                    c_res = await pkg.resolve_or_create_contact(sb, party_name, "customer")
                    zoho_contact_id = c_res.get("contact_id")
                if not zoho_contact_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": f"Customer '{party_name}' could not be resolved in Zoho Books."},
                    }
                payload = {
                    "customer_id": zoho_contact_id,
                    "account_id": from_zoho_acc,
                    "payment_mode": "cash" if from_acc["account_kind"] in ("cash_galla", "supervisor_float") else "banktransfer",
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": desc,
                    **({"date": txn_date} if txn_date else {}),
                    **order_tags,
                }
                resp = await _post_zoho_with_order_tag_fallback(
                    client, "/customerpayments", payload, has_order_tags=bool(order_tags)
                )
                if resp.is_success:
                    pid = (resp.json().get("payment") or {}).get("payment_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "customer_payment",
                        "zoho_id": pid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            if entry_kind == "vendor_payment":
                existing_id = await pkg._find_existing_by_reference(
                    client, "/vendorpayments", "vendorpayments", "payment_id", reference_number
                )
                if existing_id:
                    return {
                        "ok": True,
                        "status_code": 200,
                        "error_kind": "none",
                        "zoho_type": "vendor_payment",
                        "zoho_id": existing_id,
                        "deduped": True,
                    }
                vp_desc = desc
                if not zoho_contact_id and party_name:
                    payee = await pkg.resolve_payee_zoho_vendor(
                        sb, party_name, zoho_contact_id=zoho_contact_id, allow_adhoc_create=True
                    )
                    zoho_contact_id = payee.get("contact_id")
                    if payee.get("canonical_name") and payee.get("via_alias"):
                        vp_desc = f"[{actor_label}] {proj_prefix}{payee['canonical_name']} (via {payee['via_alias']}) {notes}".strip()[:250]
                if not zoho_contact_id:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "error_kind": "validation_400",
                        "error": {"message": f"Vendor '{party_name}' could not be resolved in Zoho Books."},
                    }
                bill_allocs = await pkg.build_fifo_vendor_bill_allocations(
                    client, str(zoho_contact_id), float(amount_inr)
                )
                payload = {
                    "vendor_id": zoho_contact_id,
                    "paid_through_account_id": from_zoho_acc,
                    "amount": round(float(amount_inr), 2),
                    "reference_number": reference_number,
                    "description": vp_desc,
                    **({"date": txn_date} if txn_date else {}),
                    **({"bills": bill_allocs} if bill_allocs else {}),
                    **order_tags,
                }
                resp = await _post_zoho_with_order_tag_fallback(
                    client, "/vendorpayments", payload, has_order_tags=bool(order_tags)
                )
                if not resp.is_success and resp.status_code == 400 and bill_allocs and "bill" in resp.text.lower():
                    no_bills_payload = {k: v for k, v in payload.items() if k != "bills"}
                    resp = await _post_zoho_with_order_tag_fallback(
                        client, "/vendorpayments", no_bills_payload, has_order_tags=bool(order_tags)
                    )
                if resp.is_success:
                    vpid = (resp.json().get("vendorpayment") or {}).get("payment_id")
                    return {
                        "ok": True,
                        "status_code": resp.status_code,
                        "error_kind": "none",
                        "zoho_type": "vendor_payment",
                        "zoho_id": vpid,
                    }
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error_kind": classify_external_error(resp.status_code),
                    "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
                }

            # Default: non-GST Management Expense (plus Loan Principal / Loan Interest)
            cat_code = normalize_or_infer_expense_category(expense_category, notes, party_name)
            if cat_code == "loan_principal":
                return await _push_loan_principal_to_zoho(
                    client,
                    from_acc=from_acc,
                    from_zoho_acc=from_zoho_acc,
                    amount_inr=amount_inr,
                    party_name=party_name,
                    notes=notes,
                    desc=desc,
                    reference_number=reference_number,
                    txn_date=txn_date,
                    account_name=account_name,
                )

            existing_id = await pkg._find_existing_by_reference(
                client, "/expenses", "expenses", "expense_id", reference_number
            )
            if existing_id:
                return {
                    "ok": True,
                    "status_code": 200,
                    "error_kind": "none",
                    "zoho_type": "expense",
                    "zoho_id": existing_id,
                    "deduped": True,
                }
            accounts = await pkg._get_chart_of_accounts(client)
            matched_coa = _pick_zoho_expense_account(accounts, cat_code, account_name=account_name)
            if not matched_coa:
                return {
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {"message": "No active Expense account found in Zoho Chart of Accounts."},
                }
            payee = (
                {}
                if cat_code == "loan_interest"
                else await pkg.resolve_payee_zoho_vendor(
                    sb, party_name, zoho_contact_id=zoho_contact_id, allow_adhoc_create=True
                )
            )
            exp_desc = desc
            if payee.get("canonical_name") and payee.get("via_alias"):
                exp_desc = (
                    f"[{actor_label}] {proj_prefix}{payee['canonical_name']} "
                    f"(via {payee['via_alias']}) {notes}"
                ).strip()[:250]
            vendor_zoho_id = payee.get("contact_id")
            customer_zoho_id = None if cat_code == "loan_interest" else (bridge.get("zoho_contact_id") if project_id else None)
            exp_payload = {
                "account_id": matched_coa["account_id"],
                "paid_through_account_id": from_zoho_acc,
                "amount": round(float(amount_inr), 2),
                "reference_number": reference_number,
                "description": exp_desc,
                "is_inclusive_tax": False,
                **({"vendor_id": vendor_zoho_id} if vendor_zoho_id else {}),
                **({"customer_id": customer_zoho_id} if customer_zoho_id else {}),
                **({"date": txn_date} if txn_date else {}),
                **({} if cat_code == "loan_interest" else order_tags),
            }
            resp = await _post_zoho_with_order_tag_fallback(
                client, "/expenses", exp_payload, has_order_tags=(bool(order_tags) and cat_code != "loan_interest")
            )
            if resp.is_success:
                eid = (resp.json().get("expense") or {}).get("expense_id")
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "error_kind": "none",
                    "zoho_type": "expense",
                    "zoho_id": eid,
                    "expense_category": cat_code,
                    "expense_account_name": matched_coa.get("account_name"),
                }
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_kind": classify_external_error(resp.status_code),
                "error": resp.json() if "json" in resp.headers.get("content-type", "") else {"message": resp.text[:240]},
            }
    except httpx.TimeoutException as exc:
        return {
            "ok": False,
            "status_code": None,
            "error_kind": "transient_5xx",
            "error": {"message": f"Zoho timeout: {exc}"},
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 503,
            "error_kind": "transient_5xx",
            "error": {"message": f"Zoho network error: {exc}"},
        }
