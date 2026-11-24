"""Stateful per-turn Draft Cart (`TurnDraftCart`) and deterministic entity enrichment (Upgrades A & B)."""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.goal_primitives import is_ambiguous_emi_text, normalize_or_infer_expense_category

_FIN_FAMILY = frozenset(
    {
        "customer_receipt",
        "customer_payment",
        "vendor_payment",
        "expense",
        "float_transfer",
        "supplier_draft_bill",
        "customer_draft_invoice",
    }
)
_INFLOW_FAMILY = frozenset({"customer_receipt", "customer_payment", "customer_draft_invoice"})


def goal_item_dedup_key(it: Any, slots: dict[str, Any] | None = None) -> str:
    """Stable deduplication key matching staged action-tool items with envelope items."""

    def _v(attr: str) -> Any:
        if slots and slots.get(attr) not in (None, "", []):
            return slots[attr]
        return getattr(it, attr, None)

    if getattr(it, "target_open_goal_id", None):
        return f"open:{it.target_open_goal_id}"
    op = str(_v("operation_kind") or "")
    if _v("item_name"):
        return f"{op}:{_v('project_id') or ''}:{str(_v('item_name')).strip().lower()}"
    if op == "new_lead" and (_v("full_name") or _v("party_name")):
        who = str(_v("full_name") or _v("party_name") or "").strip().lower()
        return f"new_lead:{who}"
    if op in ("pipeline_stage_move", "material_stage_move", "update_order_details") and _v("project_id"):
        return f"{op}:{_v('project_id')}:{_v('to_stage') or ''}"
    if op in _FIN_FAMILY:
        who = str(_v("party_name") or "").strip().lower()
        amt = _v("amount_inr")
        amt_s = f"{float(amt):g}" if amt not in (None, "") else ""
        cat_tag = f":{_v('expense_category')}" if op == "expense" and _v("expense_category") in ("loan_principal", "loan_interest") else ""
        return f"{op}{cat_tag}:{who}:{amt_s}:{_v('invoice_no') or ''}"
    return f"{op}:{str(getattr(it, 'summary_title', '') or '').strip().lower()}"


def merge_goal_intent_items(base: Any, patch: Any) -> Any:
    """Merge non-empty fields from patch onto base GoalIntentItem in-place without losing prior slots."""
    base_data = base.model_dump()
    patch_data = patch.model_dump()
    for k, v in patch_data.items():
        if k == "is_ambiguous":
            base_data[k] = bool(v)
        elif k in ("choices", "clarification_question"):
            base_data[k] = v if v else ([] if k == "choices" else None)
        elif v is not None and v != [] and v != "":
            base_data[k] = v
    return type(base).model_validate(base_data)


class TurnDraftCart:
    """Stateful per-turn workspace mapping explicit handles ('D1', 'D2', ...) to GoalIntentItem drafts."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.heard_transcript: str | None = None
        self._seq: int = 0

    def has_items(self) -> bool:
        return bool(self.entries)

    def items(self) -> list[Any]:
        raw_items = [e["item"] for e in self.entries.values()]
        leads = [it for it in raw_items if str(getattr(it, "operation_kind", "")) == "new_lead"]
        receipts = [it for it in raw_items if str(getattr(it, "operation_kind", "")) == "customer_receipt"]
        if not (leads and receipts):
            return raw_items

        merged_receipt_ids: set[int] = set()
        out: list[Any] = []
        for it in raw_items:
            if id(it) in merged_receipt_ids:
                continue
            if str(getattr(it, "operation_kind", "")) == "new_lead":
                lname = re.sub(r"[^a-z0-9]", "", str(getattr(it, "full_name", "") or "").lower())
                for rc in receipts:
                    if id(rc) in merged_receipt_ids:
                        continue
                    rname = re.sub(r"[^a-z0-9]", "", str(getattr(rc, "party_name", "") or "").lower())
                    same_cust = bool(lname and rname and (lname in rname or rname in lname)) or len(leads) == 1
                    if same_cust and not getattr(rc, "project_id", None):
                        upd: dict[str, Any] = {}
                        if getattr(rc, "amount_inr", None) and not getattr(it, "advance_amount_inr", None):
                            upd["advance_amount_inr"] = rc.amount_inr
                        if getattr(rc, "payment_account_code", None) and not getattr(it, "payment_account_code", None):
                            upd["payment_account_code"] = rc.payment_account_code
                        if getattr(rc, "notes", None) and not getattr(it, "advance_notes", None):
                            upd["advance_notes"] = rc.notes
                        if upd:
                            it = it.model_copy(update=upd)
                            merged_receipt_ids.add(id(rc))
                            break
            out.append(it)
        return [it for it in out if id(it) not in merged_receipt_ids]

    def clarified_keys(self) -> set[str]:
        return {goal_item_dedup_key(e["item"]) for e in self.entries.values() if e.get("clarified")}

    def upsert(
        self,
        sb: Any,
        cand: Any,
        *,
        requested_draft_id: str | None = None,
        allow_clarify: bool = False,
        confirm_separate_duplicate: bool = False,
        acc_code_to_id: dict[str, str],
        ui_context: dict[str, Any] | None,
        user_text: str | None,
        heard_transcript: str | None,
        reply_to_bot_msg_id: int | None = None,
        telegram_chat_id: int | None = None,
        pinned_goal_id: str | None = None,
    ) -> tuple[str, Any, list[str], dict[str, Any], bool]:
        from app.services.action_tools import preflight_goal_item_missing_slots
        from app.services.zoho.contacts import _core_name_tokens

        def _preflight(it: Any) -> tuple[list[str], dict[str, Any]]:
            m, rs = preflight_goal_item_missing_slots(
                sb,
                it,
                acc_code_to_id=acc_code_to_id,
                ui_context=ui_context,
                user_text=user_text,
                heard_transcript=heard_transcript,
                reply_to_bot_msg_id=reply_to_bot_msg_id,
                telegram_chat_id=telegram_chat_id,
                pinned_goal_id=pinned_goal_id,
            )
            if not getattr(it, "party_name", None) and rs.get("party_name"):
                it.party_name = str(rs["party_name"])
            if rs.get("operation_kind") and rs["operation_kind"] != getattr(it, "operation_kind", None):
                it.operation_kind = rs["operation_kind"]
            return m, rs

        req_did = (
            requested_draft_id.strip().upper()
            if requested_draft_id and requested_draft_id.strip()
            else None
        )
        if req_did and req_did in self.entries:
            merged = merge_goal_intent_items(self.entries[req_did]["item"], cand)
            m, rs = _preflight(merged)
            self.entries[req_did] = {
                "item": merged,
                "missing": m,
                "clarified": bool(self.entries[req_did].get("clarified") or allow_clarify),
            }
            return req_did, merged, m, rs, False

        missing, resolved_slots = _preflight(cand)
        op = str(resolved_slots.get("operation_kind") or cand.operation_kind)

        # Same-turn financial duplicate guard (blocks double-entry unless distinct payees or explicitly confirmed)
        if op in _FIN_FAMILY and not confirm_separate_duplicate:
            amt = resolved_slots.get("amount_inr")
            acc = str(resolved_slots.get("payment_account_code") or "").strip().lower()
            p_new = str(resolved_slots.get("party_name") or "").strip().lower()
            toks_new = _core_name_tokens(p_new)
            for did, ent in list(self.entries.items()):
                e_it = ent["item"]
                e_op = str(e_it.operation_kind or "")
                if e_op not in _FIN_FAMILY:
                    continue
                if (op in _INFLOW_FAMILY) != (e_op in _INFLOW_FAMILY):
                    continue
                same_amt = bool(
                    amt and e_it.amount_inr and abs(float(amt) - float(e_it.amount_inr)) < 0.01
                )
                e_acc = str(e_it.payment_account_code or "").strip().lower()
                same_or_blank_acc = (acc and e_acc and acc == e_acc) or (not acc or not e_acc)
                if same_amt and same_or_blank_acc:
                    p_old = str(e_it.party_name or "").strip().lower()
                    toks_old = _core_name_tokens(p_old)
                    c_old, c_new = str(e_it.expense_category or ""), str(resolved_slots.get("expense_category") or cand.expense_category or "")
                    distinct_loan_split = bool({"loan_principal", "loan_interest"} & {c_old, c_new} and c_old != c_new)
                    distinct_payees = distinct_loan_split or bool(
                        p_old
                        and p_new
                        and (toks_old != toks_new if (toks_old and toks_new) else p_old != p_new)
                    )
                    if not distinct_payees:
                        merged = merge_goal_intent_items(e_it, cand)
                        m2, rs2 = _preflight(merged)
                        self.entries[did] = {
                            "item": merged,
                            "missing": m2,
                            "clarified": bool(ent.get("clarified") or allow_clarify),
                        }
                        return did, merged, m2, rs2, True

        new_key = goal_item_dedup_key(cand, resolved_slots)
        for did, ent in list(self.entries.items()):
            if goal_item_dedup_key(ent["item"]) == new_key:
                merged = merge_goal_intent_items(ent["item"], cand)
                m2, rs2 = _preflight(merged)
                self.entries[did] = {
                    "item": merged,
                    "missing": m2,
                    "clarified": bool(ent.get("clarified") or allow_clarify),
                }
                return did, merged, m2, rs2, False

        incomplete_same_op = [
            (did, ent)
            for did, ent in self.entries.items()
            if ent.get("missing") and str(ent["item"].operation_kind) == op
        ]
        if len(incomplete_same_op) == 1:
            did, ent = incomplete_same_op[0]
            p_old = str(ent["item"].party_name or "").strip().lower()
            p_new = str(resolved_slots.get("party_name") or "").strip().lower()
            if not p_old or not p_new or _core_name_tokens(p_old) == _core_name_tokens(p_new):
                merged = merge_goal_intent_items(ent["item"], cand)
                m2, rs2 = _preflight(merged)
                self.entries[did] = {
                    "item": merged,
                    "missing": m2,
                    "clarified": bool(ent.get("clarified") or allow_clarify),
                }
                return did, merged, m2, rs2, False

        self._seq += 1
        new_did = f"D{self._seq}"
        self.entries[new_did] = {
            "item": cand,
            "missing": missing,
            "clarified": bool(allow_clarify),
        }
        return new_did, cand, missing, resolved_slots, False


async def enrich_candidate_entities(
    sb: Any,
    cand: Any,
    *,
    user_text: str | None = None,
    heard_transcript: str | None = None,
) -> None:
    """Deterministically match payees & customer orders inside action tools (Upgrade B)."""
    from app.services import zoho
    from app.services.agent_core.financial_amendments import apply_custodian_and_payee_guards
    from app.services.agent_core.schemas import DisambiguationChoice
    from app.services.agent_core.slot_resolvers import _accept_id

    op = str(cand.operation_kind or "")
    if op == "expense":
        item_cat = normalize_or_infer_expense_category(cand.expense_category, cand.notes, cand.summary_title, cand.party_name)
        if item_cat == "other_expense":
            item_cat = normalize_or_infer_expense_category(None, user_text, heard_transcript)
        ambig_emi = not cand.target_open_goal_id and (
            (item_cat not in ("loan_principal", "loan_interest") and is_ambiguous_emi_text(cand.notes, cand.party_name, cand.summary_title, user_text, heard_transcript))
            or (bool((user_text or "").strip() or (heard_transcript or "").strip()) and is_ambiguous_emi_text(user_text, heard_transcript))
        )
        if ambig_emi:
            cand.is_ambiguous = True
            cand.is_new_payee = False
            cand.vendor_id = None
            cand.zoho_contact_id = None
            cand.project_id = None
            cand.party_name = cand.party_name or "Loan EMI"
            cand.choices = [
                DisambiguationChoice(choice_id="emi_principal", label="🏦 Loan Principal (Liability)", slot_patch_json='{"expense_category":"loan_principal","is_ambiguous":false}'),
                DisambiguationChoice(choice_id="emi_interest", label="📉 Loan Interest (Expense)", slot_patch_json='{"expense_category":"loan_interest","is_ambiguous":false}'),
            ]
            cand.clarification_question = "🏦 Yeh *EMI* entry *Loan Principal* (Liability kam karega) hai ya *Loan Interest* (Expense)? Button chunein ya dono alag-alag amount reply karein."
            return
        if item_cat in ("loan_principal", "loan_interest"):
            cand.expense_category = item_cat
            cand.is_new_payee = False
            cand.vendor_id = None
            cand.zoho_contact_id = None
            cand.project_id = None
            if not cand.party_name:
                cand.party_name = "Loan Principal" if item_cat == "loan_principal" else "Loan Interest"
            return

    if op in ("expense", "vendor_payment", "supplier_draft_bill") and cand.party_name:
        m = await zoho.match_existing_payee(sb, cand.party_name)
        if m.get("status") == "matched":
            cand.party_name = m["canonical_name"]
            cand.is_new_payee = False
            if m.get("zoho_contact_id"):
                cand.zoho_contact_id = m["zoho_contact_id"]
            if m.get("vendor_id"):
                cand.vendor_id = m["vendor_id"]
            if m.get("person_id"):
                cand.person_id = m["person_id"]
            extra = m.get("role_hint") or (f"via {m['via_alias']}" if m.get("via_alias") else "")
            if extra and extra.lower() not in (cand.notes or "").lower():
                cand.notes = f"{extra}; {cand.notes}".strip("; ") if cand.notes else extra
        elif m.get("status") == "ambiguous":
            cand.is_ambiguous = True
            cand.choices = [DisambiguationChoice(**c) for c in (m.get("choices") or [])]
            cand.clarification_question = m.get("question")
        elif m.get("status") == "new":
            cand.is_new_payee = True

        if not cand.is_ambiguous:
            cand_dict = cand.model_dump()
            new_op, g_slots = await apply_custodian_and_payee_guards(
                sb,
                op=op,
                slots=cand_dict,
                user_text=user_text,
                heard_transcript=heard_transcript,
            )
            cand.operation_kind = new_op  # type: ignore[assignment]
            for fld in (
                "to_payment_account_code",
                "person_id",
                "vendor_id",
                "zoho_contact_id",
                "customer_id",
                "project_id",
                "is_new_payee",
                "force_direct_expense",
                "auto_create_site_order",
            ):
                setattr(cand, fld, g_slots.get(fld))
            op = new_op

    if op in ("customer_receipt", "customer_payment"):
        if cand.skip_order_link:
            cand.project_id = None
        elif not cand.order_link_confirmed:
            matched_orders: list[dict[str, Any]] = []
            if cand.project_id:
                if _accept_id(sb, cand.project_id, "projects"):
                    matched_orders = [
                        r
                        for r in (
                            sb.table("projects")
                            .select("id,project_code,customer_id,customer_name,site_area_landmark,status")
                            .eq("id", cand.project_id)
                            .limit(1)
                            .execute()
                            .data
                            or []
                        )
                        if r.get("status", "active") == "active"
                    ]
                else:
                    matched_orders = zoho.find_matching_active_orders(sb, str(cand.project_id))
            if not matched_orders and cand.party_name:
                matched_orders = zoho.find_matching_active_orders(sb, cand.party_name)

            msg_blob = f"{user_text or ''} {heard_transcript or ''}"
            code_explicit = (
                len(matched_orders) == 1
                and bool(matched_orders[0].get("project_code"))
                and bool(
                    re.search(
                        rf"(?<![A-Za-z0-9]){re.escape(str(matched_orders[0]['project_code']))}(?![A-Za-z0-9])",
                        msg_blob,
                        re.IGNORECASE,
                    )
                )
            )
            if code_explicit:
                p = matched_orders[0]
                cand.project_id = p["id"]
                cand.order_link_confirmed = True
                if p.get("customer_id"):
                    cand.customer_id = p["customer_id"]
                if p.get("customer_name"):
                    cand.party_name = p["customer_name"]
                cand.is_new_payee = False
            elif matched_orders:
                cand.project_id = None
                cand.is_ambiguous = True
                cand.is_new_payee = False
                choices: list[DisambiguationChoice] = []
                for idx, p in enumerate(matched_orders[:3]):
                    cname = str(p.get("customer_name") or "").strip()[:26]
                    lbl = f"🔗 {cname} (#{p.get('project_code') or ''})"[:48]
                    patch_obj: dict[str, Any] = {
                        "project_id": p["id"],
                        "order_link_confirmed": True,
                        "is_ambiguous": False,
                        "is_new_payee": False,
                    }
                    if p.get("customer_id"):
                        patch_obj["customer_id"] = p["customer_id"]
                    if p.get("customer_name"):
                        patch_obj["party_name"] = p["customer_name"]
                    choices.append(
                        DisambiguationChoice(
                            choice_id=f"order_{idx}",
                            label=lbl,
                            slot_patch_json=json.dumps(patch_obj, ensure_ascii=False),
                        )
                    )
                choices.append(
                    DisambiguationChoice(
                        choice_id="order_walkin",
                        label="🚶 Separate / Walk-in Sale",
                        slot_patch_json=json.dumps(
                            {"project_id": None, "skip_order_link": True, "is_ambiguous": False},
                            ensure_ascii=False,
                        ),
                    )
                )
                cand.choices = choices
                if len(matched_orders) == 1:
                    p0 = matched_orders[0]
                    cand.clarification_question = (
                        f"🔗 *{p0.get('customer_name')}* is in your Orders list "
                        f"(`#{p0.get('project_code')}`). Link this payment to that order?"
                    )
                else:
                    cand.clarification_question = (
                        f"🔗 Multiple active orders match *{cand.party_name}* — kaunse order se link karein?"
                    )

    if op in (
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "update_order_details",
        "customer_draft_invoice",
    ):
        has_valid_pid = bool(cand.project_id and _accept_id(sb, cand.project_id, "projects"))
        hint = (cand.project_id if (cand.project_id and not has_valid_pid) else None) or (
            cand.party_name if op == "customer_draft_invoice" and not has_valid_pid else None
        )
        if hint:
            om = zoho.match_existing_order_or_customer(sb, hint)
            if om.get("status") == "matched":
                cand.project_id = om["project_id"]
                if om.get("customer_id"):
                    cand.customer_id = om["customer_id"]
                if op == "customer_draft_invoice" and om.get("customer_name"):
                    cand.party_name = om["customer_name"]
                    cand.is_new_payee = False
            elif om.get("status") == "ambiguous":
                cand.is_ambiguous = True
                cand.choices = [DisambiguationChoice(**c) for c in (om.get("choices") or [])]
                cand.clarification_question = om.get("question")

    if (
        op in ("customer_receipt", "customer_payment", "customer_draft_invoice")
        and not cand.project_id
        and not cand.is_ambiguous
        and cand.party_name
    ):
        cm = await zoho.match_existing_customer(sb, cand.party_name)
        if cm.get("status") == "matched":
            cand.party_name = cm["canonical_name"]
            cand.is_new_payee = False
            if cm.get("customer_id"):
                cand.customer_id = cm["customer_id"]
            if cm.get("zoho_contact_id"):
                cand.zoho_contact_id = cm["zoho_contact_id"]
        elif cm.get("status") == "ambiguous":
            cand.is_ambiguous = True
            cand.choices = [DisambiguationChoice(**c) for c in (cm.get("choices") or [])]
            cand.clarification_question = cm.get("question")
        elif cm.get("status") == "new" and cand.is_new_payee is None:
            cand.is_new_payee = True
