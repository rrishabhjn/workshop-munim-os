from typing import Any
import google.auth
from googleapiclient.discovery import build

from app.config import get_settings

BACKUP_TABS = [
    "projects",
    "project_items",
    "item_parts",
    "item_materials",
    "cash_ledger",
    "followups",
    "v_project_pnl",
    "v_master_purchase_list",
]


def export_readonly_sheet_snapshot(sb: Any) -> dict[str, Any]:
    """Export a one-way read-only snapshot of key Supabase tables/views to Google Sheets."""
    settings = get_settings()
    if not settings.backup_sheet_id:
        return {"exported": False, "reason": "BACKUP_SHEET_ID not configured"}

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sheets_api = service.spreadsheets()

    existing_meta = sheets_api.get(spreadsheetId=settings.backup_sheet_id).execute()
    existing_titles = {
        s["properties"]["title"] for s in existing_meta.get("sheets", [])
    }

    missing = [t for t in BACKUP_TABS if t not in existing_titles]
    if missing:
        requests = [{"addSheet": {"properties": {"title": t}}} for t in missing]
        sheets_api.batchUpdate(
            spreadsheetId=settings.backup_sheet_id, body={"requests": requests}
        ).execute()

    row_counts: dict[str, int] = {}
    for tab in BACKUP_TABS:
        rows = sb.table(tab).select("*").limit(5000).execute().data or []
        row_counts[tab] = len(rows)
        if not rows:
            values = [["(empty)"]]
        else:
            headers = list(rows[0].keys())
            values = [headers] + [
                [str(r.get(h, "")) if r.get(h) is not None else "" for h in headers]
                for r in rows
            ]

        sheets_api.values().clear(
            spreadsheetId=settings.backup_sheet_id, range=f"{tab}!A:ZZ"
        ).execute()
        sheets_api.values().update(
            spreadsheetId=settings.backup_sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    return {"exported": True, "row_counts": row_counts}
