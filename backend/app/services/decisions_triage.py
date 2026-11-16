"""Surgical Front-Door Triage via Gemini Decisions API (gemini-decision-1, v1beta).

Evaluates closed-set yes/no ('noul'), 'choice', and 'score' rubric questions in a
single forward pass with 0 output tokens ($0.10/1M input, $0.00 output).

Called ONLY where it avoids waking gemini-4-hydrogen altogether or classifies media:
  1. Unaddressed group-chat relevance filter
  2. Single-pass Photo/PDF classification + OCR legibility rubric (0..2)
  3. Async post-confirm workshop_memory edit worthiness gate
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
from typing import Any, Literal

from google import genai

from app.config import get_settings
from app.services.goal_primitives import ThinkingLevel, select_hydrogen_thinking_level

logger = logging.getLogger(__name__)

MediaKind = Literal[
    "supplier_bill",
    "payment_receipt",
    "multi_txn_screenshot",
    "sizing_sheet",
    "design_or_site_photo",
]


@dataclass(frozen=True)
class MediaTriageResult:
    media_kind: MediaKind
    ocr_difficulty: float
    recommended_thinking: ThinkingLevel
    confidence: float


def _get_decisions_client() -> genai.Client:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options={"api_version": "v1beta"},
    )


def is_group_message_relevant(text: str, domain_channel: str = "production") -> bool:
    """Return True if an unaddressed group text message is an actionable workshop update or query."""
    clean = (text or "").strip()
    if not clean:
        return False
    if clean.startswith("/") or "@" in clean:
        return True

    settings = get_settings()
    if not settings.gemini_api_key:
        return any(ch.isdigit() for ch in clean)

    try:
        client = _get_decisions_client()
        resp = client.decisions.create(
            model=settings.gemini_decision_model,
            input=f"Channel={domain_channel}\nMessage: {clean[:600]}",
            questions={
                "is_workshop_relevant": {
                    "type": "noul",
                    "instructions": (
                        "Is this Hindi/Hinglish/English message reporting or asking about "
                        "furniture orders, customer leads, design/sizing/BOQ, mistri assignments, "
                        "material stock/orders, deliveries, bills, payments, cash, or balances "
                        "(as opposed to casual social greetings like 'good morning' or 'ok')?"
                    ),
                }
            },
        )
        ans = resp.answers["is_workshop_relevant"]
        return float(getattr(ans, "noul", 0.0)) >= 0.35
    except Exception as exc:
        logger.warning("Decisions API group relevance check fallback: %s", exc)
        return True


def triage_media_upload(
    file_bytes: bytes,
    mime_type: str,
    caption: str = "",
    domain_scope: str = "munim",
) -> MediaTriageResult:
    """Classify an image or PDF and score OCR legibility in one $0-output pass."""
    settings = get_settings()
    if not settings.gemini_api_key:
        fallback_kind: MediaKind = (
            "supplier_bill" if mime_type == "application/pdf" else "design_or_site_photo"
        )
        return MediaTriageResult(
            media_kind=fallback_kind,
            ocr_difficulty=1.0,
            recommended_thinking="medium",
            confidence=0.5,
        )

    b64 = base64.b64encode(file_bytes).decode("ascii")
    media_block_type = "document" if mime_type == "application/pdf" else "image"
    inputs: list[dict[str, Any]] = [
        {"type": media_block_type, "data": b64, "mime_type": mime_type},
        {
            "type": "text",
            "text": f"Channel={domain_scope} | Caption={caption[:300] or 'None'}",
        },
    ]

    try:
        client = _get_decisions_client()
        resp = client.decisions.create(
            model=settings.gemini_decision_model,
            input=inputs,
            questions={
                "media_kind": {
                    "type": "choice",
                    "instructions": "Classify what this workshop upload primarily shows.",
                    "criteria": {
                        "supplier_bill": "A printed or handwritten vendor/material invoice, GST bill, or kacchi parchi.",
                        "payment_receipt": "A single UPI / GPay / PhonePe / Paytm / NEFT / bank transfer or cash payment receipt.",
                        "multi_txn_screenshot": "A bank statement or ledger screenshot showing 2 or more separate transactions.",
                        "sizing_sheet": "A room/furniture measurement sketch, site sizing sheet, or BOQ list.",
                        "design_or_site_photo": "A furniture reference design, shop-floor progress photo, packed bundle, or site installation photo.",
                    },
                },
                "ocr_difficulty": {
                    "type": "score",
                    "instructions": "Rate how hard the text/numbers in this document are to read accurately.",
                    "criteria": [
                        "Clean digital screenshot, native PDF, or crisp computer-printed invoice.",
                        "Slightly angled, shadowed, or folded camera photo of a printed document.",
                        "Handwritten Hindi/Hinglish kacchi parchi, carbon copy, smudged ink, or blurry numbers.",
                    ],
                },
            },
        )
        kind_ans = resp.answers["media_kind"]
        diff_ans = resp.answers["ocr_difficulty"]
        chosen = str(getattr(kind_ans, "choice", "design_or_site_photo"))
        valid_kinds: set[str] = {
            "supplier_bill",
            "payment_receipt",
            "multi_txn_screenshot",
            "sizing_sheet",
            "design_or_site_photo",
        }
        media_kind: MediaKind = (
            chosen if chosen in valid_kinds else "design_or_site_photo"  # type: ignore[assignment]
        )
        score = float(getattr(diff_ans, "score", 0.5))
        conf = float(getattr(kind_ans, "confidence", 0.8))
        thinking = select_hydrogen_thinking_level(ocr_difficulty_score=score)
        return MediaTriageResult(
            media_kind=media_kind,
            ocr_difficulty=score,
            recommended_thinking=thinking,
            confidence=conf,
        )
    except Exception as exc:
        logger.warning("Decisions API media triage fallback: %s", exc)
        fallback: MediaKind = (
            "supplier_bill" if mime_type == "application/pdf" else "design_or_site_photo"
        )
        return MediaTriageResult(
            media_kind=fallback,
            ocr_difficulty=1.0,
            recommended_thinking="medium",
            confidence=0.5,
        )


def should_distill_memory_rule(prior_summary: str, user_correction: str) -> bool:
    """Return True only if a user correction teaches a general reusable workshop rule."""
    settings = get_settings()
    if not settings.gemini_api_key or not user_correction.strip():
        return False
    try:
        client = _get_decisions_client()
        resp = client.decisions.create(
            model=settings.gemini_decision_model,
            input=f"Prior draft: {prior_summary[:300]}\nUser edit: {user_correction[:300]}",
            questions={
                "is_reusable_rule": {
                    "type": "noul",
                    "instructions": (
                        "Does this user correction state a reusable rule about a person's permanent role, "
                        "proxy bank alias, account preference, or shop workflow (rather than fixing a "
                        "one-time amount, date, or typo)?"
                    ),
                }
            },
        )
        return float(getattr(resp.answers["is_reusable_rule"], "noul", 0.0)) >= 0.55
    except Exception as exc:
        logger.warning("Decisions API memory worthiness gate fallback: %s", exc)
        return False
