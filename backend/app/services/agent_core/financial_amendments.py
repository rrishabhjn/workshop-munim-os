"""Same-day IST financial undo/amendment helpers plus CUJ #3 custodian & payee guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sys
from typing import Any

from app.services import goal_intelligence, inventory_ai, step_executors, zoho
from app.services.goal_primitives import format_inr

_IST = timezone(timedelta(hours=5, minutes=30))

FINANCIAL_UNDO_OPS: frozenset[str] = frozenset(
    {
        "expense",
        "float_transfer",
        "customer_receipt",
        "customer_payment",
        "vendor_payment",
        "supplier_draft_bill",
        "customer_draft_invoice",
    }
)

GENERIC_EXPENSE_WORDS: frozenset[str] = frozenset(
    {
        "fuel", "petrol", "diesel", "cng", "food", "tea", "chai", "coffee",
        "nashta", "snacks", "lunch", "dinner", "mess", "ration", "transport",
        "tempo", "rickshaw", "auto", "freight", "bhada", "loading", "unloading",
        "factory expense", "shop expense", "office expense", "workshop expense",
        "misc", "miscellaneous", "utility", "utilities", "electricity", "water",
        "bill", "rent", "kiraya", "stationery", "tools", "consumables",
        "petty cash", "cash", "galla", "general", "other", "expense", "kharcha",
        "salary", "wages", "labour", "labor",
        "loan", "emi", "kisht", "interest", "byaaj", "byaj", "principal",
        "muldhan", "mooldhan", "loan interest", "loan principal", "rgb loan", "rgb term loan",
    }
)

_SALARY_HINT_RE = re.compile(
    r"\b(?:salary|tankhwa|wages|monthly\s+pay|fnf|payroll|vetan)\b",
    re.IGNORECASE,
)
_AMEND_CUE_RE = re.compile(
    r"\b(?:correct(?:ed|ion)?|amend|fix|update|change|actually|instead|galat|galti|mistake|"
    r"wrong|above|upar|wal[ae]|usko|usse|that\s+one|same\s+entry|"
    r"not\s+(?:cash|axis|rgb|icici|idfc|sbi|canara|paytm|expense)|"
    r"link\s+(?:to|it)|tag\s+(?:to|it)|add\s+(?:it|above|to)|also\s+for)\b",
    re.IGNORECASE,
)
_ABOVE_REF_RE = re.compile(r"\b(?:above|upar|previous|last\s+entry|usko|usse|wal[ae])\b", re.IGNORECASE)


def _pkg() -> Any:
    return sys.modules[__package__]


def _parse_utc(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_financial_goal_row(goal_row: dict[str, Any]) -> bool:
    steps = goal_row.get("steps") or []
    if any(s.get("operation") in FINANCIAL_UNDO_OPS for s in steps):
        return True
    slots = goal_row.get("collected_slots") or {}
    return slots.get("operation_kind") in FINANCIAL_UNDO_OPS


def is_goal_undoable_today_ist(
    goal_row: dict[str, Any], *, now_utc: datetime | None = None
) -> bool:
    """Return True if a completed goal may show [↩️ Undo] (same-day IST window for financial/inventory entries)."""
    if goal_row.get("status") != "completed":
        return False
    slots = goal_row.get("collected_slots") or {}
    is_inv = slots.get("operation_kind") in inventory_ai.INVENTORY_OPS or any(
        s.get("operation") in inventory_ai.INVENTORY_OPS for s in (goal_row.get("steps") or [])
    )
    has_non_fin_undo = bool(goal_row.get("undo_event_ids")) or any(
        bool((s.get("result") or {}).get("entity_undo")) for s in (goal_row.get("steps") or [])
    )
    if is_financial_goal_row(goal_row) or is_inv:
        now_dt = (now_utc or datetime.now(timezone.utc)).astimezone(_IST)
        res_dt = _parse_utc(goal_row.get("resolved_at")) or _parse_utc(goal_row.get("updated_at"))
        if res_dt is None:
            return True
        return res_dt.astimezone(_IST).date() == now_dt.date()
    return has_non_fin_undo


def build_today_completed_financial_tray(
    sb: Any, *, max_goals: int = 10, now_utc: datetime | None = None
) -> list[dict[str, Any]]:
    """Compact grounding list of same-day IST completed financial/bill entries eligible for amendment."""
    try:
        rows = (
            sb.table("agent_goals")
            .select("id,status,goal_type,summary_title,collected_slots,steps,resolved_at,updated_at")
            .eq("status", "completed")
            .order("resolved_at", desc=True)
            .limit(30)
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    tray: list[dict[str, Any]] = []
    for r in rows:
        if not is_financial_goal_row(r) or not is_goal_undoable_today_ist(r, now_utc=now_utc):
            continue
        slots = r.get("collected_slots") or {}
        fin_step = next(
            (s for s in reversed(r.get("steps") or []) if s.get("operation") in FINANCIAL_UNDO_OPS),
            {},
        )
        res = fin_step.get("result") or {}
        tray.append(
            {
                "goal_id": r["id"],
                "summary_title": r.get("summary_title"),
                "operation_kind": fin_step.get("operation") or slots.get("operation_kind"),
                "party_name": slots.get("party_name"),
                "amount_inr": slots.get("amount_inr"),
                "payment_account_code": slots.get("payment_account_code"),
                "to_payment_account_code": slots.get("to_payment_account_code"),
                "expense_category": slots.get("expense_category"),
                "project_id": slots.get("project_id"),
                "notes": slots.get("notes"),
                "zoho_id": res.get("zoho_id"),
                "resolved_at": r.get("resolved_at") or r.get("updated_at"),
            }
        )
        if len(tray) >= max_goals:
            break
    return tray


def detect_implicit_same_day_amendment(
    sb: Any,
    *,
    op: str,
    slots: dict[str, Any],
    user_text: str | None,
    heard_transcript: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Detect if an incoming financial turn amends a same-day IST completed goal."""
    if op not in FINANCIAL_UNDO_OPS:
        return slots, None
    if slots.get("skip_amend_check") or slots.get("confirm_separate_duplicate_entry"):
        return slots, None

    today_tray = build_today_completed_financial_tray(sb, max_goals=10)
    if not today_tray:
        return slots, None

    explicit_gid = str(slots.get("amends_goal_id") or "").strip()
    prior: dict[str, Any] | None = None
    if explicit_gid:
        prior = next((g for g in today_tray if str(g["goal_id"]) == explicit_gid), None)
    else:
        blob = f"{user_text or ''} {heard_transcript or ''}".strip()
        if not _AMEND_CUE_RE.search(blob):
            return slots, None

        party_low = str(slots.get("party_name") or "").strip().lower()
        amt_val = slots.get("amount_inr")
        cat_new = str(slots.get("expense_category") or "")
        for cand in today_tray:
            cat_old = str(cand.get("expense_category") or "")
            if {"loan_principal", "loan_interest"} & {cat_new, cat_old} and cat_new != cat_old and not _ABOVE_REF_RE.search(blob):
                continue
            c_party = str(cand.get("party_name") or "").strip().lower()
            c_amt = cand.get("amount_inr")
            same_party = bool(party_low and c_party and (party_low in c_party or c_party in party_low))
            same_amt = bool(
                amt_val is not None and c_amt is not None and abs(float(amt_val) - float(c_amt)) < 1.0
            )
            if same_party or same_amt:
                prior = cand
                break
        if prior is None and _ABOVE_REF_RE.search(blob):
            prior = today_tray[0]

    if prior is None:
        return slots, None

    merged = dict(slots)
    merged["amends_goal_id"] = prior["goal_id"]
    if not merged.get("amend_prior_summary"):
        p_amt = format_inr(float(prior.get("amount_inr") or 0))
        p_acc = str(prior.get("payment_account_code") or "").upper() or "-"
        merged["amend_prior_summary"] = f"{prior.get('party_name') or prior.get('operation_kind')} {p_amt} ({p_acc})"
    for inherit_key in (
        "party_name",
        "amount_inr",
        "payment_account_code",
        "expense_category",
        "project_id",
        "notes",
    ):
        if merged.get(inherit_key) in (None, "") and prior.get(inherit_key) not in (None, ""):
            merged[inherit_key] = prior[inherit_key]
    return merged, prior


async def undo_zoho_steps_and_project_snapshots(
    sb: Any, goal_row: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Reverse live Zoho financial entries + Postgres project cache snapshots for a completed goal."""
    reverted: list[str] = []
    errors: list[str] = []
    for step in reversed(goal_row.get("steps") or []):
        op = step.get("operation")
        if op not in FINANCIAL_UNDO_OPS:
            continue
        res = step.get("result") or {}
        if res.get("status") == "skipped" or not res.get("ok"):
            continue
        z_id = res.get("zoho_id")
        z_type = res.get("zoho_type")
        if not z_type:
            if op == "supplier_draft_bill":
                z_type = "bill"
            elif op == "customer_draft_invoice":
                z_type = "invoice"
            elif op == "float_transfer":
                z_type = "transfer_fund"
            elif op == "customer_receipt":
                z_type = "sales_deposit"
            else:
                z_type = op
        if z_id:
            del_res = await zoho.delete_zoho_financial_entry(zoho_id=str(z_id), zoho_type=z_type)
            if not del_res.get("ok"):
                errors.append(str(del_res.get("error") or f"Failed to void Zoho {z_type} {z_id}"))
            else:
                reverted.append(f"Voided Zoho {z_type} (`{z_id}`)")

        snap = res.get("project_undo_snapshot")
        if isinstance(snap, dict) and snap.get("project_id") and isinstance(snap.get("before"), dict):
            pid = snap["project_id"]
            before_patch = dict(snap["before"])
            if before_patch:
                sb.table("projects").update(before_patch).eq("id", pid).execute()
                reverted.append("Restored Order financial/stage snapshot")

    return reverted, errors


async def execute_void_prior_goal_step(sb: Any, *, prior_goal_id: str) -> dict[str, Any]:
    rows = sb.table("agent_goals").select("*").eq("id", prior_goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Prior goal {prior_goal_id} not found."}}
    prior_g = rows[0]
    if prior_g.get("status") == "undone":
        return {"ok": True, "voided_goal_id": prior_goal_id, "receipt": "↩️ Prior entry already voided."}
    if not is_goal_undoable_today_ist(prior_g):
        return {
            "ok": False,
            "error_kind": "validation_400",
            "error": {"message": "Only same-day IST entries can be amended automatically."},
        }
    reverted, errors = await undo_zoho_steps_and_project_snapshots(sb, prior_g)
    if errors:
        return {
            "ok": False,
            "error_kind": "transient_5xx" if goal_intelligence.is_transient_zoho_error_message("; ".join(errors)) else "validation_400",
            "error": {"message": "; ".join(errors)},
        }
    sb.table("agent_goals").update({"status": "undone"}).eq("id", prior_goal_id).execute()
    return {
        "ok": True,
        "voided_goal_id": prior_goal_id,
        "receipt": f"↩️ Replaced prior entry ({', '.join(reverted) or 'voided'})",
    }


async def execute_auto_create_site_order_step(
    sb: Any, *, slots: dict[str, Any], actor_person_id: str | None, src: str
) -> dict[str, Any]:
    cust_name = str(slots.get("site_customer_name") or slots.get("full_name") or slots.get("party_name") or "Customer").strip()
    area = str(slots.get("area_landmark") or slots.get("site_area_hint") or "Beawar").strip()
    scope_txt = str(slots.get("rough_scope") or slots.get("notes") or slots.get("summary_title") or "Site Work").strip()
    created = step_executors.create_customer_and_lead(
        sb,
        full_name=cust_name,
        area_landmark=area,
        rough_scope=scope_txt,
        quoted_total=float(slots.get("quoted_total") or 0),
        existing_customer_id=slots.get("customer_id"),
        actor_person_id=actor_person_id,
    )
    if created.get("ok") and created.get("project_id"):
        await step_executors.transition_project_pipeline(
            sb,
            project_id=str(created["project_id"]),
            to_stage="site_sizing_boq",
            actor_person_id=actor_person_id,
            source_channel=src,
        )
    return created


def dedup_or_insert_vendor_or_person(sb: Any, slots: dict[str, Any]) -> dict[str, Any]:
    """Insert or deduplicate vendor (by GSTIN, canonical_name, aliases) or add person to roster."""
    raw_kind = str(slots.get("person_or_vendor_kind") or "salaried_karigar").strip().lower()
    kind = "supplier" if raw_kind in ("vendor", "suppliers", "material_vendor", "material_supplier") else raw_kind
    name = str(slots.get("full_name") or slots.get("party_name") or "").strip()
    if not name:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Name is required."}}
    if kind in ("supplier", "contractor"):
        norm_gst = str(slots["gstin"]).strip().upper() if slots.get("gstin") else None
        existing_vendors = sb.table("vendors").select("*").execute().data or []
        matched: dict[str, Any] | None = None
        if norm_gst:
            matched = next(
                (v for v in existing_vendors if str(v.get("gstin") or "").strip().upper() == norm_gst),
                None,
            )
        if not matched:
            nlow = name.lower()
            for v in existing_vendors:
                cname = str(v.get("canonical_name") or "").strip().lower()
                als = [str(a).strip().lower() for a in (v.get("aliases") or [])]
                if cname == nlow or nlow in als:
                    matched = v
                    break
        if matched:
            patch: dict[str, Any] = {}
            new_aliases = list(dict.fromkeys([*(matched.get("aliases") or []), *(slots.get("aliases") or []), name]))
            canon = str(matched.get("canonical_name") or "")
            merged_aliases = [a for a in new_aliases if a.strip().lower() != canon.strip().lower()]
            if merged_aliases != list(matched.get("aliases") or []):
                patch["aliases"] = merged_aliases
            if slots.get("phone") and not matched.get("phone"):
                patch["phone"] = slots["phone"]
            if norm_gst and not matched.get("gstin"):
                patch["gstin"] = norm_gst
            if patch:
                sb.table("vendors").update(patch).eq("id", matched["id"]).execute()
            return {
                "ok": True,
                "deduped": True,
                "vendor_id": matched["id"],
                "receipt": f"Vendor *{canon}* already exists (merged details)",
            }

        ins = sb.table("vendors").insert(
            {
                "canonical_name": name,
                "vendor_kind": kind,
                "phone": slots.get("phone"),
                "gstin": norm_gst,
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


async def run_async_financial_or_stage_undo(
    sb: Any, *, goal_id: str, actor_person_id: str | None, sync_undo_fn: Any
) -> dict[str, Any]:
    rows = sb.table("agent_goals").select("*").eq("id", goal_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error": "Goal not found."}
    g = rows[0]
    if g.get("status") == "undone":
        return {"ok": True, "already_undone": True, "reverted": []}
    if is_financial_goal_row(g):
        if not is_goal_undoable_today_ist(g):
            return {
                "ok": False,
                "error": "Financial Undo sirf same-day IST tak available hai. Puraane entries ko directly Zoho Books ya Clerk se adjust karein.",
            }
        fin_reverted, errors = await undo_zoho_steps_and_project_snapshots(sb, g)
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        stage_reverted = step_executors.undo_stage_events(
            sb, list(g.get("undo_event_ids") or []), actor_person_id=actor_person_id
        )
        sb.table("agent_goals").update({"status": "undone"}).eq("id", goal_id).execute()
        await zoho.best_effort_sync_after_mutation(sb)
        return {"ok": True, "reverted": fin_reverted + stage_reverted}
    if any(s.get("operation") in inventory_ai.INVENTORY_OPS for s in (g.get("steps") or [])):
        if not is_goal_undoable_today_ist(g):
            return {"ok": False, "error": "Inventory Undo sirf same-day IST tak available hai."}
        inv_reverted, errors = await inventory_ai.undo_inventory_goal_steps(sb, g, actor_person_id=actor_person_id)
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        sb.table("agent_goals").update({"status": "undone"}).eq("id", goal_id).execute()
        await zoho.best_effort_sync_after_mutation(sb)
        return {"ok": True, "reverted": inv_reverted}
    return sync_undo_fn(sb, goal_id=goal_id, actor_person_id=actor_person_id)


def _find_custodian_float_account(
    sb: Any, *, person_id: str | None, party_name: str
) -> dict[str, Any] | None:
    try:
        accs = (
            sb.table("payment_accounts")
            .select("id,code,display_name,account_kind,custodian_person_id,aliases,is_active")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    floats = [a for a in accs if a.get("account_kind") == "supervisor_float"]
    if person_id:
        by_pid = next((a for a in floats if a.get("custodian_person_id") == person_id), None)
        if by_pid:
            return by_pid
    plow = re.sub(r"\s+(?:ji|bhaiya|bhai|sir)$", "", party_name.strip().lower()).strip()
    if len(plow) < 3:
        return None
    for a in floats:
        disp = str(a.get("display_name") or "").lower()
        code = str(a.get("code") or "").lower()
        aliases = [str(al).lower() for al in (a.get("aliases") or [])]
        if plow in disp or plow in code or any(plow in al or al in plow for al in aliases if len(al) >= 3):
            return a
    return None


async def _match_customer_or_site_from_context(
    sb: Any, *, party_name: str, user_text: str | None, notes: str | None
) -> dict[str, Any] | None:
    if party_name:
        cm = await zoho.match_existing_customer(sb, party_name)
        if cm.get("status") == "matched" and cm.get("customer_id"):
            return {
                "id": cm["customer_id"],
                "full_name": cm.get("canonical_name") or party_name,
                "zoho_contact_id": cm.get("zoho_contact_id"),
                "area_landmark": cm.get("area_landmark"),
            }
    blob = f" {user_text or ''} {notes or ''} ".lower()
    if not blob.strip():
        return None
    try:
        custs = (
            sb.table("customers")
            .select("id,full_name,area_landmark,aliases,zoho_contact_id,phone,is_active")
            .execute()
            .data
            or []
        )
    except Exception:
        return None
    for c in custs:
        if c.get("is_active") is False:
            continue
        names = [c.get("full_name"), *(c.get("aliases") or [])]
        for cand in names:
            clow = str(cand or "").strip().lower()
            clean_c = re.sub(r"\s+(?:ji|sir|madam|customer)$", "", clow).strip()
            if len(clean_c) >= 4 and re.search(rf"\b{re.escape(clean_c)}\b", blob):
                return c
    return None


def _find_active_orders_for_customer(sb: Any, customer_id: str) -> list[dict[str, Any]]:
    try:
        rows = (
            sb.table("projects")
            .select("id,project_code,customer_id,customer_name,site_area_landmark,pipeline_stage,status")
            .eq("customer_id", customer_id)
            .execute()
            .data
            or []
        )
    except Exception:
        return []
    return [r for r in rows if r.get("status", "active") == "active" and r.get("pipeline_stage") != "lost"]


async def apply_custodian_and_payee_guards(
    sb: Any,
    *,
    op: str,
    slots: dict[str, Any],
    user_text: str | None,
    heard_transcript: str | None,
) -> tuple[str, dict[str, Any]]:
    """Enforce CUJ #3A (wallet custodian float default) and CUJ #3B (payee & site hygiene)."""
    out_op = op
    out = dict(slots)
    if out_op == "expense" and out.get("expense_category") in ("loan_principal", "loan_interest"):
        out["vendor_id"] = None
        out["zoho_contact_id"] = None
        out["project_id"] = None
        out["is_new_payee"] = False
        return out_op, out
    party = str(out.get("party_name") or out.get("full_name") or "").strip()
    party_low = party.lower()
    notes_blob = f"{out.get('notes') or ''} {user_text or ''} {heard_transcript or ''}".strip()

    # CUJ #3A: Outgoing to an active supervisor wallet custodian defaults to float_transfer
    if out_op in ("expense", "vendor_payment") and not out.get("force_direct_expense"):
        is_explicit_salary = (
            out.get("expense_category") == "salary_wages" or bool(_SALARY_HINT_RE.search(notes_blob))
        )
        if not is_explicit_salary:
            cust_acc = _find_custodian_float_account(
                sb, person_id=out.get("person_id"), party_name=party
            )
            src_code = str(out.get("payment_account_code") or "").lower()
            if cust_acc and cust_acc["code"] != src_code:
                out_op = "float_transfer"
                out["operation_kind"] = "float_transfer"
                out["to_payment_account_code"] = cust_acc["code"]
                out["to_payment_account_id"] = cust_acc["id"]
                if cust_acc.get("custodian_person_id"):
                    out["person_id"] = cust_acc["custodian_person_id"]
                out["is_new_payee"] = False
                out["custodian_float_fallback"] = True
                return out_op, out

    # CUJ #3B (1): Generic expense nouns MUST NEVER become Zoho Vendors
    if out_op in ("expense", "vendor_payment", "supplier_draft_bill") and party_low:
        cleaned_word = re.sub(r"\b(?:ka|ke|ki|expense|kharcha|bill)\b", "", party_low).strip()
        if party_low in GENERIC_EXPENSE_WORDS or cleaned_word in GENERIC_EXPENSE_WORDS:
            out["vendor_id"] = None
            out["zoho_contact_id"] = None
            out["is_new_payee"] = False
            out["is_generic_expense_label"] = True

    # CUJ #3B (2): Customer matching & site order linking (0 / 1 / 2+ orders)
    if (
        out_op in ("expense", "supplier_draft_bill", "vendor_payment")
        and not out.get("project_id")
        and not out.get("skip_site_prompt")
    ):
        matched_cust = await _match_customer_or_site_from_context(
            sb, party_name=party, user_text=user_text or heard_transcript, notes=out.get("notes")
        )
        party_matches_vendor = bool(
            out.get("vendor_id") and not out.get("is_generic_expense_label")
        )
        if matched_cust:
            if not party_matches_vendor:
                mc_name = str(matched_cust.get("full_name") or "").strip().lower()
                if party_low and (party_low in mc_name or mc_name in party_low):
                    out["vendor_id"] = None
                    out["zoho_contact_id"] = matched_cust.get("zoho_contact_id")
                    out["is_new_payee"] = False
                    out["matched_customer_as_site"] = True
            out["customer_id"] = matched_cust["id"]
            active_orders = _find_active_orders_for_customer(sb, str(matched_cust["id"]))
            if len(active_orders) == 1:
                out["project_id"] = active_orders[0]["id"]
                out.pop("site_link_choices", None)
                out.pop("offer_create_site_order", None)
            elif len(active_orders) >= 2:
                out["site_link_choices"] = [
                    {
                        "choice_id": f"site_{idx}",
                        "label": f"🔗 {o.get('project_code') or 'Order'} ({o.get('site_area_landmark') or o.get('customer_name')})"[:48],
                        "slot_patch_json": json.dumps(
                            {"project_id": o["id"], "customer_id": matched_cust["id"], "skip_site_prompt": True},
                            ensure_ascii=False,
                        ),
                    }
                    for idx, o in enumerate(active_orders[:3])
                ] + [
                    {
                        "choice_id": "site_none",
                        "label": "Customer-Only (No Site Order)",
                        "slot_patch_json": json.dumps(
                            {"project_id": None, "customer_id": matched_cust["id"], "skip_site_prompt": True},
                            ensure_ascii=False,
                        ),
                    }
                ]
            else:
                out["offer_create_site_order"] = True
                out["site_customer_name"] = matched_cust.get("full_name")
                out["site_area_hint"] = matched_cust.get("area_landmark")

    return out_op, out


def build_financial_guard_choices(op: str, slots: dict[str, Any]) -> list[dict[str, Any]]:
    """Build 1-tap toggle/disambiguation choices for Confirm cards (amend, custodian, site link)."""
    choices: list[dict[str, Any]] = []
    if slots.get("amends_goal_id") and not slots.get("explicit_amend_tool"):
        choices.append(
            {
                "choice_id": "amend_separate",
                "label": "➕ Log Separate New Entry",
                "slot_patch_json": json.dumps(
                    {"amends_goal_id": None, "amend_prior_summary": None, "skip_amend_check": True},
                    ensure_ascii=False,
                ),
            }
        )
    if op == "float_transfer" and slots.get("custodian_float_fallback"):
        choices.append(
            {
                "choice_id": "cust_direct_exp",
                "label": "🔴 Book as Direct Expense",
                "slot_patch_json": json.dumps(
                    {
                        "operation_kind": "expense",
                        "force_direct_expense": True,
                        "to_payment_account_code": None,
                        "to_payment_account_id": None,
                        "custodian_float_fallback": None,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    if (
        op in ("expense", "supplier_draft_bill", "vendor_payment")
        and not slots.get("project_id")
        and not slots.get("skip_site_prompt")
    ):
        for sc in slots.get("site_link_choices") or []:
            choices.append(sc)
        if slots.get("offer_create_site_order") and not slots.get("auto_create_site_order"):
            choices.append(
                {
                    "choice_id": "site_create_order",
                    "label": "➕ Create Order",
                    "slot_patch_json": json.dumps({"auto_create_site_order": True}, ensure_ascii=False),
                }
            )
    return choices


async def prepare_turn_financial_amendment_and_choices(
    sb: Any,
    *,
    op: str,
    slots: dict[str, Any],
    item: Any,
    user_text: str | None,
    heard_transcript: str | None,
    choices_serialized: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Run CUJ #3 guards, implicit same-day amendment detection, and 1-tap Confirm-card buttons."""
    explicit_amend = bool(getattr(item, "amends_goal_id", None))
    op2, guarded = await apply_custodian_and_payee_guards(
        sb,
        op=op,
        slots=slots,
        user_text=user_text,
        heard_transcript=heard_transcript,
    )
    slots2, _ = detect_implicit_same_day_amendment(
        sb, op=op2, slots=guarded, user_text=user_text, heard_transcript=heard_transcript
    )
    if explicit_amend and slots2.get("amends_goal_id"):
        slots2["explicit_amend_tool"] = True

    merged_choices = list(choices_serialized)
    seen_ids = {str(c.get("choice_id") or c.get("label")) for c in merged_choices}
    for gc in build_financial_guard_choices(op2, slots2):
        k = str(gc.get("choice_id") or gc.get("label"))
        if k not in seen_ids:
            merged_choices.append(gc)
            seen_ids.add(k)
    return op2, slots2, merged_choices


def apply_financial_slot_patch_adjustments(
    sb: Any, *, merged_slots: dict[str, Any], slot_patch: dict[str, Any]
) -> None:
    """Clear explicitly nulled keys, handle force_direct_expense, and sync payment account IDs."""
    for k, v in slot_patch.items():
        if v is None:
            merged_slots.pop(k, None)
    if slot_patch.get("force_direct_expense"):
        merged_slots["operation_kind"] = "expense"
        merged_slots.pop("to_payment_account_code", None)
        merged_slots.pop("to_payment_account_id", None)
        merged_slots.pop("custodian_float_fallback", None)
    if "payment_account_code" in slot_patch or "to_payment_account_code" in slot_patch:
        try:
            acc_rows = (
                sb.table("payment_accounts").select("id,code").eq("is_active", True).execute().data or []
            )
            acc_map = {str(a["code"]).lower(): a["id"] for a in acc_rows if a.get("code")}
            p_code = str(merged_slots.get("payment_account_code") or "").lower()
            if p_code in acc_map:
                merged_slots["payment_account_id"] = acc_map[p_code]
            t_code = str(merged_slots.get("to_payment_account_code") or "").lower()
            if t_code in acc_map:
                merged_slots["to_payment_account_id"] = acc_map[t_code]
        except Exception:
            pass
