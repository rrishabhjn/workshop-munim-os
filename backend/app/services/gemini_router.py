"""Batch supplier bill OCR extractor (used by scripts/batch_archive_ocr.py)."""

import json
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.config import get_settings


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


def _build_bill_grounding_context(sb: Any) -> str:
    """Load canonical suppliers and active projects for legacy bill batch OCR."""
    vendors: list[dict[str, Any]] = []
    try:
        vendors = (
            sb.table("vendors")
            .select("canonical_name,vendor_kind,aliases,gstin")
            .eq("is_active", True)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        vendors = []

    entities: list[dict[str, Any]] = []
    try:
        entities = (
            sb.table("entity_dictionary")
            .select("canonical_name,entity_type,aliases,phone,notes")
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        entities = []

    projects = (
        sb.table("projects")
        .select("id,project_code,customer_name,status,project_type")
        .eq("status", "active")
        .limit(100)
        .execute()
        .data
        or []
    )
    return json.dumps(
        {
            "own_firm_names": ["The Furniture Company", "The Work App", "Shreyansh Jain"],
            "vendors": vendors,
            "entity_dictionary": entities,
            "active_projects": projects,
        },
        ensure_ascii=False,
    )


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
    grounding_json = _build_bill_grounding_context(sb)
    model_name = settings.gemini_lite_model if use_lite else settings.gemini_live_model

    prompt = (
        "Extract supplier bill details accurately for Zoho Books India Draft Bill creation. "
        "Note: 'The Furniture Company', 'The Work App', and 'Shreyansh Jain' are OUR OWN firm/owner names (the buyer) — "
        "never set supplier_name to those. "
        "Normalize supplier_name using vendors / entity_dictionary if a close alias matches. "
        "If the caption mentions a specific customer project, set matched_project_id. "
        "If it's a multi-project bulk bill or general shop stock, match the 'GEN-STOCK' project_id.\n\n"
        f"CAPTION: {caption or 'None'}\n"
        f"GROUNDING_CONTEXT:\n{grounding_json}"
    )

    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SupplierBillExtraction,
        ),
    )
    return _parse_gemini_json(response, SupplierBillExtraction)
