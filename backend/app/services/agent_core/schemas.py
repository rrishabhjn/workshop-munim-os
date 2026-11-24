"""Structured Pydantic envelopes, JSON Schema inlining, and Gemini output normalization."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_NULL_STRINGS = {"", "null", "none", "nil", "n/a", "na", "undefined", "unknown", "tbd"}
_LIST_FIELDS = {"aliases", "payout_aliases", "role_tags", "estimator_ids", "choices", "repair_options", "goal_items"}
_FLOAT_FIELDS = {"amount_inr", "quoted_total", "advance_amount_inr", "quantity", "actual_qty", "unit_rate_inr"}
_JSON_STRING_FIELDS = {"material_lines_json", "bill_line_items_json", "slot_patch_json", "initial_items_json", "inventory_lines_json"}


def _strip_json_fences(raw_text: str) -> str:
    txt = (raw_text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _coerce_optional_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    txt = str(val).strip()
    if not txt or txt.lower() in _NULL_STRINGS:
        return None
    cleaned = txt.replace(",", "")
    m_lakh = re.match(r"^(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)\b", cleaned, re.IGNORECASE)
    if m_lakh:
        return round(float(m_lakh.group(1)) * 100000.0, 2)
    m_k = re.match(r"^(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*k\b", cleaned, re.IGNORECASE)
    if m_k:
        return round(float(m_k.group(1)) * 1000.0, 2)
    m_num = re.search(r"(-?\d+(?:\.\d+)?)", cleaned)
    if m_num:
        try:
            return float(m_num.group(1))
        except ValueError:
            return None
    return None


def _coerce_optional_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if val is None:
        return None
    low = str(val).strip().lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    return None


def _sanitize_gemini_nulls(raw: Any) -> Any:
    """Normalize literal 'null', formatted numbers/dates/lists, and fences in LLM JSON outputs."""
    if not isinstance(raw, dict):
        return raw
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _FLOAT_FIELDS:
            cleaned[k] = _coerce_optional_float(v)
            continue
        if k == "is_ambiguous":
            cleaned[k] = bool(_coerce_optional_bool(v))
            continue
        if k in ("boq_scope_changed", "is_new_payee", "force_direct_expense", "auto_create_site_order", "skip_amend_check"):
            cleaned[k] = _coerce_optional_bool(v)
            continue
        if k in _JSON_STRING_FIELDS:
            if isinstance(v, (list, dict)):
                cleaned[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, str):
                unwrapped = _strip_json_fences(v)
                if unwrapped.lower() in _NULL_STRINGS:
                    cleaned[k] = "{}" if k == "slot_patch_json" else None
                else:
                    cleaned[k] = unwrapped
            else:
                cleaned[k] = "{}" if k == "slot_patch_json" else None
            continue
        if k in _LIST_FIELDS:
            if v is None:
                cleaned[k] = []
            elif isinstance(v, str):
                if v.strip().lower() in _NULL_STRINGS:
                    cleaned[k] = []
                else:
                    cleaned[k] = [
                        part.strip()
                        for part in v.split(",")
                        if part.strip() and part.strip().lower() not in _NULL_STRINGS
                    ]
            elif isinstance(v, list):
                cleaned[k] = [
                    item
                    for item in v
                    if item is not None
                    and not (isinstance(item, str) and item.strip().lower() in _NULL_STRINGS)
                ]
            else:
                cleaned[k] = []
            continue

        if isinstance(v, str) and v.strip().lower() in _NULL_STRINGS:
            cleaned[k] = None
        elif isinstance(v, list):
            cleaned[k] = [
                item
                for item in v
                if item is not None
                and not (isinstance(item, str) and item.strip().lower() in _NULL_STRINGS)
            ]
        else:
            cleaned[k] = v
    return cleaned


class DisambiguationChoice(BaseModel):
    choice_id: str
    label: str = Field(description="Short button label (<42 chars), e.g. 'Active: Vikas Sharma (Civil Lines)'")
    slot_patch_json: str = Field(
        default="{}",
        description="JSON object string of slots to merge if user taps this button (e.g. '{\"project_id\":\"...\"}').",
    )

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class GoalIntentItem(BaseModel):
    target_open_goal_id: str | None = Field(
        default=None,
        description="Set if this action answers/updates one of OPEN_GOALS; null for a brand-new task.",
    )
    operation_kind: Literal[
        "new_lead",
        "add_customer",
        "update_customer",
        "update_vendor",
        "update_person",
        "update_order_details",
        "archive_record",
        "pipeline_stage_move",
        "material_stage_move",
        "item_upsert_or_move",
        "item_material_upsert",
        "customer_receipt",
        "customer_payment",
        "vendor_payment",
        "expense",
        "float_transfer",
        "supplier_draft_bill",
        "customer_draft_invoice",
        "add_person_or_vendor",
        "inventory_load",
        "inventory_attribution",
        "inventory_count_adjust",
    ]
    summary_title: str = Field(description="Crisp 1-line summary in Hinglish/English.")
    is_ambiguous: bool = Field(
        default=False,
        description="True if multiple homonym customers/projects/people match or target is unclear.",
    )
    clarification_question: str | None = Field(
        default=None,
        description="ONE consolidated polite Hinglish/English question asking for ALL missing/ambiguous info at once.",
    )
    choices: list[DisambiguationChoice] = Field(default_factory=list)

    # Production, Master Spine & Customer Slots
    project_id: str | None = None
    customer_id: str | None = None
    vendor_id: str | None = None
    person_id: str | None = None
    full_name: str | None = Field(
        default=None,
        description="REQUIRED for new_lead / add_customer / add_person_or_vendor: customer, person, or vendor name (e.g. 'Arjun').",
    )
    new_name: str | None = Field(
        default=None,
        description="Populate ONLY when explicitly renaming an existing Customer, Vendor, or Person.",
    )
    area_landmark: str | None = Field(
        default=None,
        description=(
            "REQUIRED for new_lead (and customer/order area updates): city, locality, or site landmark "
            "(e.g. 'Jaipur', 'Civil Lines, Jaipur', 'Beawar'). CRITICAL: Always populate this field directly — "
            "never put the city/area only inside summary_title."
        ),
    )
    rough_scope: str | None = Field(
        default=None,
        description="REQUIRED for new_lead: rooms or furniture items scope (e.g. '3 Bedrooms — Beds & Almirahs').",
    )
    phone: str | None = None
    gstin: str | None = None
    specialty: str | None = None
    aliases: list[str] = Field(default_factory=list)
    payout_aliases: list[str] = Field(
        default_factory=list,
        description="Worker proxy bank payee names (e.g. ['Ruksar Bano'] for Lala Ji).",
    )
    role_tags: list[str] = Field(default_factory=list)
    archive_entity_kind: str | None = Field(
        default=None,
        description="For archive_record ('delete'/'remove' requests): 'customer' | 'vendor' | 'person' | 'order'.",
    )
    promised_delivery_date: str | None = None
    quoted_total: float | None = None
    to_stage: str | None = None
    material_stage: str | None = Field(
        default=None,
        description="Optional material track stage for new_lead ('check_stock' = material needed/to order, 'ordered_pending' = ordered, 'all_in_shop' = in shop).",
    )
    estimator_ids: list[str] = Field(default_factory=list)
    sizing_mistri_id: str | None = None
    boq_scope_changed: bool | None = None
    boq_scope_note: str | None = None
    item_id: str | None = None
    item_name: str | None = Field(
        default=None,
        description=(
            "REQUIRED for item_upsert_or_move (and optional filter for item_material_upsert): exact furniture item name "
            "(e.g. 'Mom Room (1) - 10ft Bed', 'Kitchen', 'Master Wardrobe'). CRITICAL: Always populate item_name directly — "
            "never put the furniture name only inside summary_title."
        ),
    )
    dimensions: str | None = Field(
        default=None,
        description="Furniture item dimensions/sizes when known (e.g. '42\"x89\"', 'Mattress 6ft x 6\\'6\", Back 10ft').",
    )
    specs_notes: str | None = Field(
        default=None,
        description="Production/material/laminate/fabric/hardware specifications from text or PDF (e.g. '19mm 15-layer ply, Hafele hinges, Sainik LS 5134').",
    )
    initial_items_json: str | None = Field(
        default=None,
        description=(
            'For new_lead or update_order_details when a production PDF/photo or multi-item list specifies concrete furniture items: '
            'JSON array string of [{"item_name":"Mom Room (1) - 10ft Bed","dimensions":"Mattress 6ft x 6\'6\\", Back 10ft","specs_notes":"32mm 15-layer ply, Shade 133/230, Sleepwell foam"}] '
            'so every furniture item is auto-created under project_items.'
        ),
    )
    production_mistri_id: str | None = None
    assembly_mistri_id: str | None = None
    material_lines_json: str | None = Field(
        default=None,
        description='JSON array string of [{"material_name":"18mm HDHMR Ply","quantity":8,"unit":"sheets","status":"needed"}] for item_material_upsert.',
    )
    material_status: str | None = Field(
        default=None,
        description="Target material status ('needed' = To Order, 'ordered' = Ordered, 'in_shop' = In Shop) when updating item material status.",
    )

    # Munim Financial & Bill Slots
    amount_inr: float | None = Field(
        default=None,
        description="REQUIRED for money entries: numeric INR amount (e.g. 20000 for ₹20,000 or 50000 for 50k).",
    )
    payment_account_code: str | None = Field(
        default=None,
        description="REQUIRED for money entries: exact account code from PAYMENT_ACCOUNTS (e.g. 'cash_galla', 'axis', 'rgb', 'ccrgb', 'paytm', 'float_ansh', 'float_kamlesh', 'float_rohit', 'float_shreyansh').",
    )
    to_payment_account_code: str | None = None
    expense_category: str | None = Field(
        default=None,
        description="For expense (including salaries/karigar/loan repayments): 'loan_principal' | 'loan_interest' | 'salary_wages' | 'contractor_labor' | 'raw_materials' | 'transport_freight' | 'food_tea_mess' | 'rent' | 'utilities_office' | 'repairs_maintenance' | 'other_expense'.",
    )
    party_name: str | None = Field(
        default=None,
        description="Customer, vendor, or payee name for receipts, bills, or payouts.",
    )
    zoho_contact_id: str | None = None
    is_new_payee: bool | None = None
    order_link_confirmed: bool | None = None
    skip_order_link: bool | None = None
    is_explicit_advance: bool | None = None
    txn_date: str | None = None
    invoice_no: str | None = None
    account_name: str | None = Field(
        default=None,
        description="Exact Zoho Chart of Accounts name override for expenses or supplier bills (from workshop_memory or error repair).",
    )
    advance_amount_inr: float | None = Field(
        default=None,
        description="Initial customer advance amount in INR when creating a new order with advance in one message.",
    )
    advance_notes: str | None = None
    amends_goal_id: str | None = Field(
        default=None,
        description="UUID of a same-day completed financial goal being amended/corrected.",
    )
    force_direct_expense: bool | None = None
    auto_create_site_order: bool | None = None
    skip_amend_check: bool | None = None
    bill_line_items_json: str | None = Field(
        default=None,
        description="JSON array string of [{description, quantity, rate, amount}] for supplier_draft_bill.",
    )
    person_or_vendor_kind: str | None = Field(
        default=None,
        description="For add_person_or_vendor: 'salaried_karigar' | 'contractor_mistri' | 'supplier' | 'contractor'",
    )
    inventory_item_id: str | None = None
    settlement_mode: str | None = None
    matched_item_material_id: str | None = None
    quantity: float | None = None
    actual_qty: float | None = None
    unit_rate_inr: float | None = None
    inventory_lines_json: str | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class AgentTurnEnvelope(BaseModel):
    heard_transcript: str | None = Field(
        default=None,
        description="Exact Hinglish/Hindi transcript when audio input is provided; null for text-only.",
    )
    intent_category: Literal[
        "goal_actions",
        "direct_answer",
        "draft_whatsapp_message",
    ]
    direct_reply_text: str | None = Field(
        default=None,
        description="Direct answer for Q&A, standalone WhatsApp customer draft, or brief note.",
    )
    referenced_project_id: str | None = Field(
        default=None,
        description="If answering a question about a specific order, populate its project_id UUID from CONTEXT.",
    )
    referenced_customer_id: str | None = Field(
        default=None,
        description="If answering a question about a specific customer, populate their customer_id UUID.",
    )
    goal_items: list[GoalIntentItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class ErrorRepairProposal(BaseModel):
    explanation_hinglish: str = Field(
        description="2-line plain Hinglish/English explanation of why Zoho or Supabase rejected the step."
    )
    repair_options: list[DisambiguationChoice] = Field(
        default_factory=list,
        description="1 to 2 concrete 1-tap repair options patching slots to fix the validation issue.",
    )

    @model_validator(mode="before")
    @classmethod
    def _clean_null_strings(cls, data: Any) -> Any:
        return _sanitize_gemini_nulls(data)


class MemoryRuleOutput(BaseModel):
    rule_text: str = Field(max_length=160)
    category: Literal["entity", "account", "workflow", "style"]


def _inline_json_schema(
    model_cls: type[BaseModel], force_required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Inline Pydantic $defs, flatten nullable anyOfs, and strip unsupported keywords."""
    raw = deepcopy(model_cls.model_json_schema())
    defs = raw.pop("$defs", {}) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = str(node["$ref"]).split("/")[-1]
                return _resolve(deepcopy(defs.get(ref_name, {})))
            if "anyOf" in node:
                non_null = [
                    x
                    for x in node["anyOf"]
                    if not (isinstance(x, dict) and x.get("type") == "null")
                ]
                if len(non_null) == 1:
                    merged = _resolve(non_null[0])
                    if isinstance(merged, dict) and "description" in node:
                        merged.setdefault("description", node["description"])
                    return merged
            cleaned: dict[str, Any] = {}
            for k, v in node.items():
                if k in ("title", "default"):
                    continue
                cleaned[k] = _resolve(v)
            if (
                cleaned.get("type") == "object"
                and isinstance(cleaned.get("properties"), dict)
                and "choice_id" in cleaned["properties"]
                and "slot_patch_json" in cleaned["properties"]
            ):
                cleaned["required"] = list(
                    dict.fromkeys([*(cleaned.get("required") or []), "slot_patch_json"])
                )
            return cleaned
        if isinstance(node, list):
            return [_resolve(x) for x in node]
        return node

    out = _resolve(raw)
    if force_required and isinstance(out, dict):
        out["required"] = list(
            dict.fromkeys([*(out.get("required") or []), *force_required])
        )
    return out
