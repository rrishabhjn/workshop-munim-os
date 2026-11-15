import json
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import get_settings


class ProposedAction(BaseModel):
    action_type: Literal[
        "create_project",
        "create_item",
        "update_stage",
        "add_material",
        "log_cash",
        "add_followup",
        "attach_photos",
        "upsert_entity",
    ]
    summary: str = Field(
        description="Short human-readable Hinglish/English summary for confirmation button card."
    )
    project_id: str | None = None
    project_label: str | None = None
    customer_name: str | None = None
    phone: str | None = None
    promised_delivery_date: str | None = Field(
        default=None, description="Strict ISO YYYY-MM-DD date if mentioned."
    )
    quoted_total: float | None = None
    item_id: str | None = None
    item_label: str | None = None
    dimensions: str | None = None
    assigned_karigar: str | None = None
    stage_code: str | None = None
    material_category: str | None = None
    material_name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    preferred_supplier: str | None = None
    txn_type: Literal[
        "customer_receipt",
        "vendor_payment",
        "karigar_labor",
        "local_transport",
        "overhead_expense",
        "cash_advance",
        "cash_balance",
        "misc",
    ] | None = None
    needs_txn_clarification: bool | None = Field(
        default=None,
        description="True only if action_type='log_cash' and the transaction category is genuinely ambiguous.",
    )
    direction: Literal["in", "out"] | None = None
    party_or_karigar: str | None = None
    amount_inr: float | None = None
    payment_mode: str | None = None
    due_date: str | None = Field(
        default=None, description="Strict ISO YYYY-MM-DD date if mentioned."
    )
    entity_type: str | None = None
    followup_name: str | None = None
    action_note: str | None = None
    notes: str | None = Field(
        default=None,
        description="Full transaction context/caveats for log_cash, or reusable facts for upsert_entity.",
    )


class VoiceCaptureResult(BaseModel):
    intent: Literal[
        "mutation",
        "question",
        "draft_customer_message",
        "clarification_needed",
        "unknown",
    ]
    transcript_summary: str = Field(
        description="1-line summary of what the user asked or reported."
    )
    answer_text: str | None = Field(
        default=None,
        description="Direct answer for Q&A or standalone copy-pasteable Hindi/Hinglish WhatsApp message.",
    )
    actions: list[ProposedAction] = Field(default_factory=list)


class BillLineItem(BaseModel):
    description: str
    quantity: float
    rate: float
    amount: float


class SupplierBillExtraction(BaseModel):
    supplier_name: str
    gstin: str | None = None
    invoice_no: str | None = None
    bill_date: str | None = Field(default=None, description="YYYY-MM-DD if visible")
    taxable_amount: float
    gst_amount: float
    total_amount: float
    matched_project_id: str | None = None
    notes: str | None = None
    line_items: list[BillLineItem] = Field(default_factory=list)


class PhotoClassificationResult(BaseModel):
    photo_kind: Literal[
        "supplier_bill",
        "payment_receipt",
        "design_photo",
        "multiple_transactions",
    ]
    reason: str = Field(description="1-line explanation of what the image shows.")
    payment_action: ProposedAction | None = Field(
        default=None,
        description="Populated when photo_kind == 'payment_receipt' (action_type='log_cash').",
    )


def _parse_gemini_json(response: Any, model_cls: type[BaseModel]) -> Any:
    raw_text = getattr(response, "text", None)
    if not raw_text or not str(raw_text).strip():
        raise RuntimeError("Gemini returned an empty response — please resend your message.")
    return model_cls.model_validate_json(raw_text)


def _get_genai_client() -> genai.Client:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    return genai.Client(api_key=settings.gemini_api_key)


def build_grounding_context(
    sb: Any, group_scope: Literal["floor", "accounts", "dm"] = "dm"
) -> str:
    """Load lexicon, active projects/items, purchase list, followups, and (for accounts/dm) live P&L."""
    stages = sb.table("workshop_stages").select("code,label").order("sort_order").execute().data or []
    cats = sb.table("material_categories").select("code,label").order("sort_order").execute().data or []
    entities = (
        sb.table("entity_dictionary")
        .select("canonical_name,entity_type,aliases,phone,notes")
        .limit(200)
        .execute()
        .data
        or []
    )
    projects = (
        sb.table("projects")
        .select("id,project_code,customer_name,promised_delivery_date,status,project_type")
        .eq("status", "active")
        .limit(100)
        .execute()
        .data
        or []
    )
    items = (
        sb.table("project_items")
        .select("id,project_id,item_name,stage,assigned_karigar,target_date")
        .limit(300)
        .execute()
        .data
        or []
    )
    purchase_list = (
        sb.table("v_master_purchase_list")
        .select("category_label,material_name,unit,total_qty_needed,item_count")
        .limit(100)
        .execute()
        .data
        or []
    )
    open_followups = (
        sb.table("followups")
        .select("due_date,entity_type,name,action_note")
        .eq("status", "open")
        .order("due_date")
        .limit(50)
        .execute()
        .data
        or []
    )

    from datetime import datetime, timedelta, timezone
    today_ist = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
    context_obj: dict[str, Any] = {
        "today_ist": today_ist,
        "group_scope": group_scope,
        "own_firm_names": ["The Furniture Company", "The Work App", "Shreyansh Jain"],
        "workshop_stages": stages,
        "material_categories": cats,
        "entity_dictionary": entities,
        "active_projects": projects,
        "active_items": items,
        "master_purchase_list": purchase_list,
        "open_followups": open_followups,
    }

    if group_scope in ("accounts", "dm"):
        pnl_rows = (
            sb.table("v_project_pnl")
            .select("project_code,customer_name,quoted_total,total_revenue,total_cost,net_profit,balance_pending,project_type")
            .eq("status", "active")
            .limit(100)
            .execute()
            .data
            or []
        )
        cash_rows = (
            sb.table("cash_ledger")
            .select("direction,amount_inr")
            .limit(1000)
            .execute()
            .data
            or []
        )
        cash_balance = sum(
            float(r["amount_inr"]) if r["direction"] == "in" else -float(r["amount_inr"])
            for r in cash_rows
        )
        context_obj["project_financials"] = pnl_rows
        context_obj["cash_box_balance_inr"] = round(cash_balance, 2)

    learned_rules: list[dict[str, Any]] = []
    try:
        learned_rules = (
            sb.table("workshop_memory")
            .select("category,rule_text")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(25)
            .execute()
            .data
            or []
        )
    except Exception:
        learned_rules = []

    recent_examples: list[dict[str, Any]] = []
    try:
        confirmed_rows = (
            sb.table("telegram_state")
            .select("raw_transcript,pending_actions")
            .eq("status", "confirmed")
            .order("created_at", desc=True)
            .limit(6)
            .execute()
            .data
            or []
        )
        for row in confirmed_rows:
            tr = (row.get("raw_transcript") or "").strip()
            acts = (row.get("pending_actions") or {}).get("actions") or []
            if tr and acts:
                recent_examples.append(
                    {
                        "user": tr[:180],
                        "actions": [
                            {
                                k: a.get(k)
                                for k in ("action_type", "txn_type", "direction", "party_or_karigar", "amount_inr", "summary")
                                if a.get(k) is not None
                            }
                            for a in acts[:3]
                        ],
                    }
                )
    except Exception:
        recent_examples = []

    context_obj["learned_rules"] = learned_rules
    context_obj["recent_confirmed_examples"] = recent_examples

    return json.dumps(context_obj, ensure_ascii=False)


def analyze_message_with_gemini(
    sb: Any,
    text: str | None = None,
    audio_bytes: bytes | None = None,
    audio_mime_type: str = "audio/ogg",
    thinking_level: str = "low",
    group_scope: Literal["floor", "accounts", "dm"] = "dm",
    prior_context: dict[str, Any] | None = None,
) -> VoiceCaptureResult:
    """Route Hinglish voice note or text through gemini-4-hydrogen with live SQL-grounded context."""
    settings = get_settings()
    client = _get_genai_client()
    grounding_json = build_grounding_context(sb, group_scope=group_scope)

    scope_rule = (
        "THIS IS THE WORKSHOP FLOOR GROUP: Keep all answers strictly PRICE-FREE. "
        "NEVER mention ₹ amounts, quotes, invoices, cash balances, or profit margins. "
        "If someone asks a financial/payment question here, tell them to ask in the Munim & Accounts group."
        if group_scope == "floor"
        else "This is an Accounts/Owner chat: you may answer financial, P&L, cash-box, and receivable questions using project_financials and cash_box_balance_inr."
    )

    system_instruction = (
        "You are Workshop Munim, an internal operations assistant for a custom furniture "
        "manufacturing workshop in Beawar, Rajasthan, India. Users speak/type in Hinglish/Hindi "
        "with local furniture terms (karigar, mistri, sunmica, ply, udhaar, baki, polish). "
        f"{scope_rule}\n"
        "Use GROUNDING_CONTEXT (entities including their notes, projects, items, learned_rules, and recent_confirmed_examples) "
        "to resolve customer names, karigars, suppliers, project_id, item_id, stage_code, and material_category deterministically.\n"
        "IMPORTANT: 'The Furniture Company', 'The Work App', and 'Shreyansh Jain' (listed in own_firm_names) are OUR OWN business/firm/owner names — never treat them as an external customer, supplier, or karigar.\n"
        "GOAL: Your job is to complete the user's entry (financial, customer, item, material, or follow-up).\n"
        "- GUESS + CONFIRM WHEN CORE INFO IS PRESENT: If party + amount (for log_cash) or core fields are known, infer the best txn_type/project using entities (and their notes), learned_rules, and recent_confirmed_examples, and return intent='mutation' so the user can 1-tap Confirm (or tap Edit if wrong).\n"
        "- ASK WHEN CRITICAL INFO IS MISSING: If a required field (e.g., amount or who paid/received) is genuinely missing, set intent='clarification_needed', put 1 short natural Hinglish/English question in answer_text, and keep any partial actions[] in actions.\n"
        "- RETAIN ALL USER-SHARED CONTEXT: Never drop extra details the user shares (e.g., 'Suresh Ji is a painter', 'for Sharma Ji bed', 'will send bill later'). Put all transaction-specific details in notes/action_note on log_cash so they are saved in cash_ledger.notes and Zoho description.\n"
        "- LEARN REUSABLE ENTITY FACTS: Whenever the user states a persistent fact about a person/supplier/karigar/customer (e.g., 'Suresh Ji is a painter who helps regularly, not labor'), ALSO emit an action_type='upsert_entity' in actions[] with party_or_karigar (canonical name), entity_type ('supplier'|'karigar'|'customer'), notes (the reusable fact), and summary (e.g. 'Remember: Suresh Ji (supplier) — Regular painter').\n"
        "- If the user announces a brand new customer order, use action_type='create_project' (with customer_name, promised_delivery_date, quoted_total) "
        "and/or action_type='create_item' (with item_name, dimensions, assigned_karigar, project_id or customer_name).\n"
        "- If the user reports progress, needed materials, cash paid/received, or a follow-up, "
        "set intent='mutation' and populate actions[].\n"
        "  For log_cash actions, classify txn_type as one of: "
        "'customer_receipt' (received from customer), 'vendor_payment' (paid to raw material/service supplier), "
        "'karigar_labor' (paid to employee/karigar/mistri), 'local_transport' (logistics/tempo/auto/freight), "
        "'overhead_expense' (rent, interest, EMI, electricity, water, fuel), or 'misc'. "
        "Set needs_txn_clarification=true ONLY if the party is unknown and category is genuinely ambiguous.\n"
        "- If the user asks a question about status, deliveries, materials (master_purchase_list), or dues, "
        "set intent='question' and answer directly using the numbers in GROUNDING_CONTEXT.\n"
        "- If the user asks to draft a message for a customer/supplier, set intent='draft_customer_message' "
        "and put ONLY the polite Hindi/Hinglish message in answer_text with no preamble.\n"
        "- If PRIOR_PENDING_DRAFT is provided below, the user is replying to clarify or edit that draft: "
        "merge their correction/answer into the accumulated thread (prior_actions + prior_messages) and return intent='mutation' with the updated actions[] (or 'clarification_needed' if still incomplete).\n\n"
        f"GROUNDING_CONTEXT:\n{grounding_json}"
        + (f"\n\nPRIOR_PENDING_DRAFT:\n{json.dumps(prior_context, ensure_ascii=False)}" if prior_context else "")
    )

    contents: list[Any] = []
    if audio_bytes:
        contents.append(
            types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime_type)
        )
    if text:
        contents.append(text)
    if not contents:
        contents.append("Empty input.")

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=VoiceCaptureResult,
    )

    response = client.models.generate_content(
        model=settings.gemini_live_model,
        contents=contents,
        config=config,
    )
    return _parse_gemini_json(response, VoiceCaptureResult)


def extract_supplier_bill(
    sb: Any,
    file_bytes: bytes,
    mime_type: str,
    caption: str | None = None,
    use_lite: bool = False,
) -> SupplierBillExtraction:
    """Extract structured GST bill fields from a photo or PDF."""
    settings = get_settings()
    client = _get_genai_client()
    grounding_json = build_grounding_context(sb, group_scope="accounts")
    model_name = settings.gemini_lite_model if use_lite else settings.gemini_live_model

    prompt = (
        "Extract supplier bill details accurately for Zoho Books India Draft Bill creation. "
        "Note: 'The Furniture Company', 'The Work App', and 'Shreyansh Jain' are OUR OWN firm/owner names (the buyer) — "
        "never set supplier_name to those. "
        "Normalize supplier_name using entity_dictionary if a close alias matches. "
        "If the caption mentions a specific customer project, set matched_project_id. "
        "If it's a multi-project bulk bill or general shop stock, match the 'GEN-STOCK' project_id.\n\n"
        f"CAPTION: {caption or 'None'}\n"
        f"GROUNDING_CONTEXT:\n{grounding_json}"
    )

    config_kwargs: dict[str, Any] = {
        "response_mime_type": "application/json",
        "response_schema": SupplierBillExtraction,
    }

    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(**config_kwargs),
    )
    return _parse_gemini_json(response, SupplierBillExtraction)


def classify_accounts_photo(
    sb: Any,
    file_bytes: bytes,
    mime_type: str,
    caption: str | None = None,
) -> PhotoClassificationResult:
    """Classify a single photo in Accounts/DM into bill, single payment receipt, design photo, or multi-txn."""
    settings = get_settings()
    client = _get_genai_client()
    grounding_json = build_grounding_context(sb, group_scope="accounts")

    prompt = (
        "Classify this single photo from a custom furniture workshop's Accounts chat:\n"
        "IMPORTANT: 'The Furniture Company', 'The Work App', and 'Shreyansh Jain' are OUR OWN business/firm/owner names. "
        "Never treat them as the external counterparty (party_or_karigar / supplier_name); use them to infer direction ('out' if paid by us, 'in' if received by us).\n"
        "- 'supplier_bill': a printed or handwritten supplier/material bill or GST invoice.\n"
        "- 'payment_receipt': a SINGLE UPI / GPay / PhonePe / Paytm / bank transfer / cash slip. "
        "Set payment_action with action_type='log_cash', direction ('in' if money received, 'out' if paid), "
        "txn_type ('customer_receipt', 'vendor_payment', 'karigar_labor', 'local_transport', 'overhead_expense', or 'misc'), "
        "needs_txn_clarification=true if party is unknown and category is ambiguous, "
        "party_or_karigar (the external person/vendor, normalized via entity_dictionary if matched), amount_inr, "
        "payment_mode ('upi', 'bank', or 'cash'), due_date (ISO YYYY-MM-DD transaction date if visible), "
        "project_id (if matched), and a concise summary.\n"
        "- 'multiple_transactions': a screenshot or list containing 2 or more separate payment/ledger rows.\n"
        "- 'design_photo': a furniture design, workshop progress, or reference photo.\n\n"
        f"CAPTION: {caption or 'None'}\n"
        f"GROUNDING_CONTEXT:\n{grounding_json}"
    )

    response = client.models.generate_content(
        model=settings.gemini_live_model,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PhotoClassificationResult,
        ),
    )
    return _parse_gemini_json(response, PhotoClassificationResult)


class LearnedRuleExtraction(BaseModel):
    should_save: bool
    category: Literal["style", "entity", "classification", "workflow"]
    rule_text: str | None = Field(
        default=None,
        description="Concise 1-line reusable rule for future entries if should_save=true.",
    )


def distill_learning_rule(
    prior_summary: str,
    correction_text: str,
    final_actions: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Distill a concise reusable rule when the user corrects a bot guess via Edit/reply."""
    settings = get_settings()
    if not settings.gemini_api_key or not correction_text.strip():
        return None
    try:
        client = _get_genai_client()
        prompt = (
            "The user corrected a bot draft in Workshop Munim OS. "
            "If this correction reveals a reusable pattern (terminology, party role, payment classification, or communication style), "
            "set should_save=true and write a concise 1-line rule_text (max 140 chars). "
            "If it is just a one-off typo/number fix with no general rule, set should_save=false.\n\n"
            f"PRIOR DRAFT: {prior_summary}\n"
            f"USER CORRECTION: {correction_text}\n"
            f"FINAL ACTIONS: {json.dumps(final_actions, ensure_ascii=False)}"
        )
        resp = client.models.generate_content(
            model=settings.gemini_lite_model,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=LearnedRuleExtraction,
            ),
        )
        parsed: LearnedRuleExtraction = _parse_gemini_json(resp, LearnedRuleExtraction)
        if parsed.should_save and parsed.rule_text and parsed.rule_text.strip():
            return {"category": parsed.category, "rule_text": parsed.rule_text.strip()[:200]}
    except Exception:
        return None
    return None
