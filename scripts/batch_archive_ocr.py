"""One-time batch OCR script for pre-cutoff historical bills & invoices.

Usage:
    python scripts/batch_archive_ocr.py --input-dir ./legacy_bills --doc-type supplier_bill

Uses gemini-3.5-flash-lite by default (with gemini-4-hydrogen fallback on low confidence),
compresses images to WebP, uploads to private GCS bucket under historical_archive/,
and inserts searchable rows into Supabase historical_archive.
"""

import argparse
from datetime import datetime, timezone
import mimetypes
from pathlib import Path
import sys

# Allow importing backend/app modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import get_supabase_admin
from app.services import gemini_router, storage


def process_folder(input_dir: Path, doc_type: str, use_live_model: bool = False) -> None:
    sb = get_supabase_admin()
    files = [p for p in input_dir.rglob("*") if p.is_file()]
    print(f"Found {len(files)} file(s) in {input_dir}")

    for idx, path in enumerate(files, start=1):
        mime_type, _ = mimetypes.guess_type(path.name)
        mime_type = mime_type or "application/octet-stream"
        if not (mime_type.startswith("image/") or mime_type == "application/pdf"):
            print(f"[{idx}/{len(files)}] Skipping unsupported file: {path.name}")
            continue

        raw_bytes = path.read_bytes()
        try:
            extracted = gemini_router.extract_supplier_bill(
                sb,
                file_bytes=raw_bytes,
                mime_type=mime_type,
                use_lite=not use_live_model,
            )
        except Exception as exc:
            print(f"[{idx}/{len(files)}] Lite OCR failed on {path.name}, retrying with live model: {exc}")
            extracted = gemini_router.extract_supplier_bill(
                sb, file_bytes=raw_bytes, mime_type=mime_type, use_lite=False
            )

        ext = "pdf" if mime_type == "application/pdf" else "webp"
        safe_party = "".join(c if c.isalnum() else "_" for c in extracted.supplier_name)[:30]
        safe_inv = "".join(c if c.isalnum() else "_" for c in (extracted.invoice_no or "noinv"))[:20]
        date_prefix = extracted.bill_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        obj_path = f"historical_archive/{doc_type}/{date_prefix}_{safe_party}_{safe_inv}_{idx}.{ext}"

        if ext == "webp":
            storage.upload_image_as_webp(obj_path, raw_bytes)
        else:
            storage.upload_bytes(obj_path, raw_bytes, content_type=mime_type)

        sb.table("historical_archive").insert(
            {
                "doc_type": doc_type,
                "doc_date": extracted.bill_date,
                "party_name": extracted.supplier_name,
                "invoice_no": extracted.invoice_no,
                "amount_inr": round(extracted.total_amount, 2),
                "gst_amount": round(extracted.gst_amount, 2),
                "storage_path": obj_path,
                "extracted_items": [li.model_dump() for li in extracted.line_items],
            }
        ).execute()
        print(f"[{idx}/{len(files)}] Archived: {extracted.supplier_name} | {extracted.invoice_no} | ₹{extracted.total_amount}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch OCR legacy workshop bills")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--doc-type", default="supplier_bill", choices=["supplier_bill", "customer_invoice", "estimate", "receipt", "other"])
    parser.add_argument("--use-live-model", action="store_true", help="Force gemini-4-hydrogen for messy handwritten slips")
    args = parser.parse_args()
    process_folder(args.input_dir, args.doc_type, args.use_live_model)
