"""Generate a formula-driven, multi-tab Workshop Munim OS Excel workbook for Shreyansh.

Connects read-only to Supabase (`get_supabase_admin()`), pulls canonical live data
across customers, orders, furniture items, raw materials, team/mistris, vendors,
and Zoho-synced payment accounts, and writes:
    exports/Workshop_Munim_Tracker_Shreyansh.xlsx

Uses strictly Excel-2007+ formulas (SUMIFS, COUNTIFS, INDEX/MATCH, IFERROR, MAX, TODAY)
and range-based DataValidation so desktop Excel opens cleanly without repair dialogs
or #NAME? errors.
"""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import sys
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
OUTPUT_PATH = ROOT_DIR / "exports" / "Workshop_Munim_Tracker_Shreyansh.xlsx"

os.chdir(BACKEND_DIR)
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_supabase_admin  # noqa: E402

# Formats & Palette (Warm Workshop Amber / Espresso theme)
INR_FMT = "[$₹-4009]#,##,##0"
QTY_FMT = "#,##0.##"
DATE_FMT = "dd-mmm-yyyy"

C_ESPRESSO = "451A03"
C_AMBER_DARK = "78350F"
C_AMBER_MID = "B45309"
C_AMBER_SOFT = "FEF3C7"
C_CREAM = "FFFBEB"
C_STRIPE = "FAFAF9"
C_BORDER = "D6D3D1"
C_TEXT = "1C1917"
C_MUTED = "57534E"

C_GREEN_BG = "DCFCE7"
C_GREEN_FG = "166534"
C_AMBER_BG = "FEF3C7"
C_AMBER_FG = "92400E"
C_BLUE_BG = "DBEAFE"
C_BLUE_FG = "1E40AF"
C_ROSE_BG = "FFE4E6"
C_ROSE_FG = "9F1239"
C_GRAY_BG = "F5F5F4"
C_GRAY_FG = "44403C"

THIN_SIDE = Side(style="thin", color=C_BORDER)
MED_AMBER = Side(style="medium", color=C_AMBER_DARK)
CELL_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
TOTAL_BORDER = Border(top=Side(style="thin", color=C_TEXT), bottom=Side(style="double", color=C_TEXT), left=THIN_SIDE, right=THIN_SIDE)

FONT_TITLE = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
FONT_SUBTITLE = Font(name="Calibri", size=10, italic=True, color=C_MUTED)
FONT_SEC = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
FONT_TH = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
FONT_BODY = Font(name="Calibri", size=10, color=C_TEXT)
FONT_BOLD = Font(name="Calibri", size=10, bold=True, color=C_TEXT)
FONT_KPI_LBL = Font(name="Calibri", size=9, bold=True, color=C_AMBER_DARK)
FONT_KPI_VAL = Font(name="Calibri", size=15, bold=True, color=C_ESPRESSO)

FILL_TITLE = PatternFill("solid", fgColor=C_ESPRESSO)
FILL_SUBTITLE = PatternFill("solid", fgColor=C_CREAM)
FILL_CREAM = FILL_SUBTITLE
FILL_SEC = PatternFill("solid", fgColor=C_AMBER_DARK)
FILL_TH = PatternFill("solid", fgColor=C_AMBER_DARK)
FILL_TH_ALT = PatternFill("solid", fgColor=C_AMBER_MID)
FILL_STRIPE = PatternFill("solid", fgColor=C_STRIPE)
FILL_WHITE = PatternFill("solid", fgColor="FFFFFF")
FILL_SOFT = PatternFill("solid", fgColor=C_AMBER_SOFT)
FILL_TOTAL = PatternFill("solid", fgColor="E7E5E4")


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(txt[:10])
        except ValueError:
            return None


def _style_banner(ws: Any, max_col: int, title: str, subtitle: str) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max_col)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max_col)
    c1 = ws.cell(1, 1, title)
    c1.font = FONT_TITLE
    c1.fill = FILL_TITLE
    c1.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 28

    c2 = ws.cell(2, 1, subtitle)
    c2.font = FONT_SUBTITLE
    c2.fill = FILL_SUBTITLE
    c2.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
    ws.row_dimensions[2].height = 22


def _add_list_dv(ws: Any, formula_ref: str, cell_range: str) -> None:
    dv = DataValidation(type="list", formula1=f"={formula_ref}", allow_blank=True)
    dv.errorStyle = "warning"
    ws.add_data_validation(dv)
    dv.add(cell_range)


def _apply_pill_cf(ws: Any, col_range: str, pairs: list[tuple[str, str, str]]) -> None:
    for val, bg, fg in pairs:
        ws.conditional_formatting.add(
            col_range,
            CellIsRule(
                operator="equal",
                formula=[f'"{val}"'],
                fill=PatternFill("solid", fgColor=bg),
                font=Font(name="Calibri", size=10, bold=True, color=fg),
            ),
        )


def _setup_print(ws: Any, landscape: bool = True) -> None:
    ws.views.sheetView[0].showGridLines = True
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE if landscape else ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def build_workbook() -> Path:
    sb = get_supabase_admin()

    customers = sb.table("customers").select("*").order("full_name").execute().data or []
    projects = sb.table("projects").select("*").order("project_code").execute().data or []
    items = sb.table("project_items").select("*").order("created_at").execute().data or []
    materials = sb.table("item_materials").select("*").order("created_at").execute().data or []
    people = sb.table("people").select("*").order("full_name").execute().data or []
    vendors = sb.table("vendors").select("*").order("canonical_name").execute().data or []
    accounts = sb.table("payment_accounts").select("*").eq("is_active", True).order("sort_order").execute().data or []

    l_pipeline = sb.table("project_pipeline_stages").select("*").order("sort_order").execute().data or []
    l_mat_track = sb.table("material_track_stages").select("*").order("sort_order").execute().data or []
    l_pay_track = sb.table("payment_track_stages").select("*").order("sort_order").execute().data or []
    l_item_stage = sb.table("workshop_stages").select("*").order("sort_order").execute().data or []
    l_mat_cat = sb.table("material_categories").select("*").order("sort_order").execute().data or []

    pipe_map = {r["code"]: r["label"] for r in l_pipeline}
    mat_track_map = {r["code"]: r["label"] for r in l_mat_track}
    pay_track_map = {r["code"]: r["label"] for r in l_pay_track}
    item_stage_map = {r["code"]: r["label"] for r in l_item_stage}
    mat_cat_map = {r["code"]: r["label"] for r in l_mat_cat}

    mat_status_map = {"needed": "To Order", "ordered": "Ordered", "in_shop": "In Shop"}
    order_status_map = {"active": "Active", "completed": "Completed", "on_hold": "On Hold", "lost": "Lost"}

    cust_by_id = {str(c["id"]): c for c in customers}
    person_by_id = {str(p["id"]): p for p in people}
    proj_by_id = {str(p["id"]): p for p in projects}
    item_by_id = {str(it["id"]): it for it in items}

    def p_name(pid: Any) -> str:
        if not pid:
            return ""
        row = person_by_id.get(str(pid))
        return str(row["full_name"]) if row else ""

    def p_names(pids: list[Any] | None) -> str:
        names = [p_name(x) for x in (pids or []) if p_name(x)]
        return ", ".join(names)

    wb = Workbook()
    ws_dash = wb.active
    ws_dash.title = "1_Dashboard_Queues"
    ws_orders = wb.create_sheet("2_Orders_Master")
    ws_items = wb.create_sheet("3_Furniture_Items")
    ws_mats = wb.create_sheet("4_Raw_Materials")
    ws_job = wb.create_sheet("5_Karigar_Job_Card")
    ws_ledger = wb.create_sheet("6_Money_Ledger")
    ws_cust = wb.create_sheet("7_Customers")
    ws_lists = wb.create_sheet("8_Team_Vendors_Lists")

    tab_colors = {
        ws_dash: "451A03",
        ws_orders: "92400E",
        ws_items: "B45309",
        ws_mats: "0F766E",
        ws_job: "1E40AF",
        ws_ledger: "166534",
        ws_cust: "6D28D9",
        ws_lists: "44403C",
    }
    for ws_t, clr in tab_colors.items():
        ws_t.sheet_properties.tabColor = clr

    # =========================================================================
    # TAB 8: 8_Team_Vendors_Lists (Team Roster, Vendors, Dropdown Lists)
    # =========================================================================
    _style_banner(
        ws_lists,
        24,
        "Team Roster, Vendors Directory & Dropdown Lists",
        "Master People & Vendors synced from Supabase. Validation columns N–X drive dropdowns across all sheets.",
    )
    ws_lists.merge_cells("A4:E4")
    c_ppl_sec = ws_lists.cell(4, 1, "Team & Mistri Roster (people)")
    c_ppl_sec.font = FONT_SEC
    c_ppl_sec.fill = FILL_SEC
    c_ppl_sec.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    ws_lists.merge_cells("G4:L4")
    c_vnd_sec = ws_lists.cell(4, 7, "Vendors & Contractors (vendors)")
    c_vnd_sec.font = FONT_SEC
    c_vnd_sec.fill = FILL_SEC
    c_vnd_sec.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    ws_lists.merge_cells("N4:X4")
    c_val = ws_lists.cell(4, 14, "Master Dropdown Reference Columns (Do Not Shift Columns N–X)")
    c_val.font = FONT_SEC
    c_val.fill = FILL_TH_ALT
    c_val.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws_lists.row_dimensions[4].height = 22

    roster_headers = ["Name", "Type", "Roles", "Phone", "Active"]

    for idx, h in enumerate(roster_headers, 1):
        cell = ws_lists.cell(5, idx, h)
        cell.font = FONT_TH
        cell.fill = FILL_TH
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = CELL_BORDER

    vendor_headers = ["Vendor Name", "Kind", "Specialty", "Phone", "GSTIN", "Active"]
    for off, h in enumerate(vendor_headers):
        cell = ws_lists.cell(5, 7 + off, h)
        cell.font = FONT_TH
        cell.fill = FILL_TH
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = CELL_BORDER

    person_type_label = {
        "operator": "Operator (Office)",
        "salaried_karigar": "Salaried Karigar",
        "contractor_mistri": "Contractor Mistri",
    }
    for i, p in enumerate(people):
        r = 6 + i
        vals = [
            p.get("full_name") or "",
            person_type_label.get(p.get("person_type") or "", p.get("person_type") or ""),
            ", ".join(p.get("role_tags") or []),
            p.get("phone") or "",
            "Yes" if p.get("is_active", True) else "No",
        ]
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        for col_i, val in enumerate(vals, 1):
            c = ws_lists.cell(r, col_i, val)
            c.font = FONT_BOLD if col_i == 1 else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            c.alignment = Alignment(vertical="center", horizontal="center" if col_i in (2, 5) else "left")

    vendor_kind_label = {"supplier": "Supplier", "contractor": "Contractor"}
    for i, v in enumerate(vendors):
        r = 6 + i
        vals = [
            v.get("canonical_name") or "",
            vendor_kind_label.get(v.get("vendor_kind") or "", v.get("vendor_kind") or ""),
            v.get("specialty") or "",
            v.get("phone") or "",
            v.get("gstin") or "",
            "Yes" if v.get("is_active", True) else "No",
        ]
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        for off, val in enumerate(vals):
            c = ws_lists.cell(r, 7 + off, val)
            c.font = FONT_BOLD if off == 0 else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            c.alignment = Alignment(vertical="center", horizontal="center" if off in (1, 5) else "left")

    pipeline_labels = [r["label"] for r in l_pipeline] or [
        "Lead", "Design", "Estimating", "Quoted to Customer / Awaiting Advance",
        "Site Sizing / BOQ", "Execution", "Completed", "On Hold", "Lost / Dropped",
    ]
    mat_track_labels = [r["label"] for r in l_mat_track] or ["Not Started", "Check Stock", "Ordered (Pending)", "All In Shop"]
    pay_track_labels = [r["label"] for r in l_pay_track] or ["Advance Pending", "Advance In", "Partial / Pre-Dispatch", "Payment Complete"]
    item_stage_labels = [r["label"] for r in l_item_stage] or ["In Shop", "Packed / Ready", "At Site", "QC Done"]
    mat_cat_labels = [r["label"] for r in l_mat_cat] or ["Ply / Wood", "Laminate / Sunmica", "Hardware", "Foam / Fabric", "Polish / Adhesive"]
    mat_status_labels = ["To Order", "Ordered", "In Shop"]
    unit_labels = ["sheets", "pcs", "sets", "kg", "ltr", "rft", "sqft", "boxes"]
    order_status_labels = ["Active", "Completed", "On Hold", "Lost"]
    txn_type_labels = ["Customer Receipt", "Expense", "Vendor Payment", "Wallet Transfer", "Supplier Bill (Draft)"]
    account_labels = [str(a["display_name"]) for a in accounts]
    yes_no_labels = ["Yes", "No"]

    val_columns: list[tuple[int, str, list[str]]] = [
        (14, "Pipeline Stages", pipeline_labels),       # N
        (15, "Material Track", mat_track_labels),       # O
        (16, "Payment Track", pay_track_labels),        # P
        (17, "Item Stages", item_stage_labels),         # Q
        (18, "Mat Status", mat_status_labels),          # R
        (19, "Mat Categories", mat_cat_labels),         # S
        (20, "Units", unit_labels),                     # T
        (21, "Order Status", order_status_labels),      # U
        (22, "Txn Types", txn_type_labels),             # V
        (23, "Accounts & Wallets", account_labels),     # W
        (24, "Yes / No", yes_no_labels),                # X
    ]
    for col_idx, hdr, vals in val_columns:
        hc = ws_lists.cell(5, col_idx, hdr)
        hc.font = FONT_TH
        hc.fill = FILL_TH_ALT
        hc.alignment = Alignment(horizontal="center", vertical="center")
        hc.border = CELL_BORDER
        for row_off, v in enumerate(vals):
            c = ws_lists.cell(6 + row_off, col_idx, v)
            c.font = FONT_BODY
            c.border = CELL_BORDER
            c.alignment = Alignment(vertical="center")

    ws_lists.row_dimensions[5].height = 24
    ws_lists.freeze_panes = "A6"
    widths_lists = {
        1: 24, 2: 18, 3: 24, 4: 15, 5: 9, 6: 3,
        7: 26, 8: 13, 9: 22, 10: 15, 11: 18, 12: 9, 13: 3,
        14: 34, 15: 20, 16: 22, 17: 16, 18: 13, 19: 20, 20: 10, 21: 13, 22: 20, 23: 22, 24: 10,
    }
    for col_i, w in widths_lists.items():
        ws_lists.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_lists)

    # Validation formula ranges (data starts at row 6!)
    R_PEOPLE = "'8_Team_Vendors_Lists'!$A$6:$A$150"
    R_VENDORS = "'8_Team_Vendors_Lists'!$G$6:$G$200"
    R_PIPE_STAGES = f"'8_Team_Vendors_Lists'!$N$6:$N${5 + len(pipeline_labels)}"
    R_MAT_TRACK = f"'8_Team_Vendors_Lists'!$O$6:$O${5 + len(mat_track_labels)}"
    R_ITEM_STAGES = f"'8_Team_Vendors_Lists'!$Q$6:$Q${5 + len(item_stage_labels)}"
    R_MAT_STATUS = f"'8_Team_Vendors_Lists'!$R$6:$R${5 + len(mat_status_labels)}"
    R_MAT_CATS = f"'8_Team_Vendors_Lists'!$S$6:$S${5 + len(mat_cat_labels)}"
    R_UNITS = f"'8_Team_Vendors_Lists'!$T$6:$T${5 + len(unit_labels)}"
    R_ORDER_STATUS = f"'8_Team_Vendors_Lists'!$U$6:$U${5 + len(order_status_labels)}"
    R_TXN_TYPES = f"'8_Team_Vendors_Lists'!$V$6:$V${5 + len(txn_type_labels)}"
    R_ACCOUNTS = "'8_Team_Vendors_Lists'!$W$6:$W$25"
    R_YESNO = "'8_Team_Vendors_Lists'!$X$6:$X$7"
    R_CUSTOMERS = "'7_Customers'!$A$5:$A$250"
    R_ORDERS = "'2_Orders_Master'!$A$6:$A$150"

    # =========================================================================
    # TAB 7: 7_Customers (Canonical Customers + Rollups)
    # =========================================================================
    _style_banner(
        ws_cust,
        11,
        "Customers Master — Client Directory & Live Receivables Rollup",
        "Canonical customers synced from Supabase. Total Orders, Received ₹, and Pending Balance roll up automatically from 2_Orders_Master.",
    )
    cust_headers = [
        "Customer Name", "Area / Landmark", "Phone", "Voice / Name Aliases",
        "Active", "Total Orders", "Active Orders", "Total Quoted (₹)",
        "Total Received (₹)", "Balance Pending (₹)", "Notes",
    ]
    ws_cust.row_dimensions[4].height = 26
    for idx, h in enumerate(cust_headers, 1):
        c = ws_cust.cell(4, idx, h)
        c.font = FONT_TH
        c.fill = FILL_TH
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = CELL_BORDER

    max_cust_row = max(150, len(customers) + 30)
    for i in range(max_cust_row - 4):
        r = 5 + i
        ws_cust.row_dimensions[r].height = 22
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        c_row = customers[i] if i < len(customers) else None

        ws_cust.cell(r, 1, c_row["full_name"] if c_row else None)
        ws_cust.cell(r, 2, (c_row.get("area_landmark") or "") if c_row else None)
        ws_cust.cell(r, 3, (c_row.get("phone") or "") if c_row else None)
        ws_cust.cell(r, 4, (", ".join(c_row.get("aliases") or [])) if c_row else None)
        ws_cust.cell(r, 5, ("Yes" if c_row.get("is_active", True) else "No") if c_row else "Yes")
        ws_cust.cell(r, 6, f'=IF(A{r}="","",COUNTIF(\'2_Orders_Master\'!$C$6:$C$150,A{r}))')
        ws_cust.cell(r, 7, f'=IF(A{r}="","",COUNTIFS(\'2_Orders_Master\'!$C$6:$C$150,A{r},\'2_Orders_Master\'!$B$6:$B$150,"Active"))')
        ws_cust.cell(r, 8, f'=IF(A{r}="","",SUMIFS(\'2_Orders_Master\'!$T$6:$T$150,\'2_Orders_Master\'!$C$6:$C$150,A{r}))')
        ws_cust.cell(r, 9, f'=IF(A{r}="","",SUMIFS(\'2_Orders_Master\'!$W$6:$W$150,\'2_Orders_Master\'!$C$6:$C$150,A{r}))')
        ws_cust.cell(r, 10, f'=IF(A{r}="","",SUMIFS(\'2_Orders_Master\'!$X$6:$X$150,\'2_Orders_Master\'!$C$6:$C$150,A{r}))')
        ws_cust.cell(r, 11, (c_row.get("notes") or "") if c_row else None)

        for col_i in range(1, 12):
            cell = ws_cust.cell(r, col_i)
            cell.font = FONT_BOLD if col_i in (1, 10) else FONT_BODY
            cell.fill = fill
            cell.border = CELL_BORDER
            if col_i in (8, 9, 10):
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif col_i in (5, 6, 7):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=(col_i == 11))

    _add_list_dv(ws_cust, R_YESNO, f"E5:E{max_cust_row}")
    ws_cust.auto_filter.ref = f"A4:K{max_cust_row}"
    ws_cust.freeze_panes = "B5"
    for col_i, w in enumerate([24, 22, 15, 22, 8, 12, 13, 17, 17, 18, 28], 1):
        ws_cust.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_cust)

    # =========================================================================
    # TAB 2: 2_Orders_Master (All Orders Board + 3 Tracks + P&L)
    # =========================================================================
    _style_banner(
        ws_orders,
        29,
        "All Orders Board — Pipeline Stage, Material Track, Payment Track & Per-Order P&L",
        "Track every customer order end-to-end. Log receipts, expenses, and supplier bills in 6_Money_Ledger to auto-update Received ₹, Pending ₹, Payment Track & Net Margin.",
    )
    band_groups = [
        (1, 6, "ORDER IDENTITY", C_ESPRESSO),
        (7, 14, "TIER 1: PROJECT PIPELINE & SCHEDULE", C_AMBER_DARK),
        (15, 19, "MATERIAL & ITEM EXECUTION TRACK", "0F766E"),
        (20, 29, "PAYMENT TRACK & ORDER P&L (₹)", "166534"),
    ]
    ws_orders.row_dimensions[4].height = 20
    for sc, ec, label, clr in band_groups:
        ws_orders.merge_cells(start_row=4, start_column=sc, end_row=4, end_column=ec)
        c = ws_orders.cell(4, sc, label)
        c.font = FONT_SEC
        c.fill = PatternFill("solid", fgColor=clr)
        c.alignment = Alignment(horizontal="center", vertical="center")

    order_headers = [
        "Order Code", "Status", "Customer Name", "Site / Area", "Phone", "Rough Scope",
        "Pipeline Stage", "Current Owner", "Estimator(s)", "Sizing Mistri", "BOQ Changed",
        "Stage Date", "Days in Stage", "Promised Delivery",
        "Material Track", "Material Owner", "Mat To Order", "Mat Ordered", "Items (QC / Total)",
        "Quoted Value (₹)", "Opening Paid (₹)", "New Receipts (₹)", "Total Received (₹)",
        "Balance Pending (₹)", "Payment Track", "Opening Cost (₹)", "Direct Cost (₹)",
        "Net Margin (₹)", "Notes / BOQ Scope Note",
    ]
    ws_orders.row_dimensions[5].height = 30
    for idx, h in enumerate(order_headers, 1):
        c = ws_orders.cell(5, idx, h)
        c.font = FONT_TH
        c.fill = FILL_TH_ALT if idx >= 20 else FILL_TH
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = CELL_BORDER

    sorted_projects = sorted(
        projects,
        key=lambda p: (
            0 if p.get("status") == "active" else 1,
            0 if p.get("project_type") != "general_stock" else 1,
            str(p.get("project_code") or ""),
        ),
    )
    max_order_row = 150
    for i in range(max_order_row - 5):
        r = 6 + i
        ws_orders.row_dimensions[r].height = 24
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        p = sorted_projects[i] if i < len(sorted_projects) else None

        if p:
            cid = str(p.get("customer_id") or "")
            canon_cust = cust_by_id.get(cid, {}).get("full_name") or p.get("customer_name") or ""
            notes_parts = []
            if p.get("boq_scope_note"):
                notes_parts.append(str(p["boq_scope_note"]))
            meta_note = (p.get("metadata") or {}).get("reconciliation_note")
            if meta_note:
                notes_parts.append(str(meta_note))

            ws_orders.cell(r, 1, p.get("project_code") or "")
            ws_orders.cell(r, 2, order_status_map.get(p.get("status") or "active", "Active"))
            ws_orders.cell(r, 3, canon_cust)
            ws_orders.cell(r, 4, p.get("site_area_landmark") or "")
            ws_orders.cell(r, 5, p.get("phone") or cust_by_id.get(cid, {}).get("phone") or "")
            ws_orders.cell(r, 6, p.get("rough_scope") or "")
            ws_orders.cell(r, 7, pipe_map.get(p.get("pipeline_stage") or "lead", "Lead"))
            ws_orders.cell(r, 8, p_names(p.get("current_owner_ids")))
            ws_orders.cell(r, 9, p_names(p.get("estimator_ids")))
            ws_orders.cell(r, 10, p_name(p.get("sizing_mistri_id")))
            ws_orders.cell(r, 11, "Yes" if p.get("boq_scope_changed") else "No")
            ws_orders.cell(r, 12, _parse_date(p.get("pipeline_stage_at")) or _parse_date(p.get("created_at")))
            ws_orders.cell(r, 14, _parse_date(p.get("promised_delivery_date")))
            ws_orders.cell(r, 15, mat_track_map.get(p.get("material_stage") or "not_started", "Not Started"))
            ws_orders.cell(r, 16, p_name(p.get("material_owner_id")) or "Ansh")
            ws_orders.cell(r, 20, round(float(p.get("quoted_total") or 0), 2))
            ws_orders.cell(r, 21, round(float(max(float(p.get("paid_cache_inr") or 0), float(p.get("zoho_invoiced_cache") or 0))), 2))
            ws_orders.cell(r, 26, round(float(max(float(p.get("cost_cache_inr") or 0), float(p.get("zoho_bills_cache") or 0))), 2))
            ws_orders.cell(r, 29, " | ".join(notes_parts))
        else:
            ws_orders.cell(r, 11, "No")

        ws_orders.cell(r, 13, f'=IF(OR(A{r}="",B{r}<>"Active",L{r}=""),0,MAX(0,TODAY()-L{r}))')
        ws_orders.cell(r, 17, f'=IF(A{r}="","",COUNTIFS(\'4_Raw_Materials\'!$A$6:$A$400,A{r},\'4_Raw_Materials\'!$H$6:$H$400,"To Order"))')
        ws_orders.cell(r, 18, f'=IF(A{r}="","",COUNTIFS(\'4_Raw_Materials\'!$A$6:$A$400,A{r},\'4_Raw_Materials\'!$H$6:$H$400,"Ordered"))')
        ws_orders.cell(r, 19, f'=IF(A{r}="","",IF(COUNTIF(\'3_Furniture_Items\'!$A$6:$A$300,A{r})=0,"0 / 0",COUNTIFS(\'3_Furniture_Items\'!$A$6:$A$300,A{r},\'3_Furniture_Items\'!$G$6:$G$300,"QC Done")&" / "&COUNTIF(\'3_Furniture_Items\'!$A$6:$A$300,A{r})))')
        ws_orders.cell(r, 22, f'=IF(A{r}="","",SUMIFS(\'6_Money_Ledger\'!$H$6:$H$500,\'6_Money_Ledger\'!$E$6:$E$500,A{r},\'6_Money_Ledger\'!$B$6:$B$500,"Customer Receipt"))')
        ws_orders.cell(r, 23, f'=IF(A{r}="","",N(U{r})+N(V{r}))')
        ws_orders.cell(r, 24, f'=IF(A{r}="","",IF(N(T{r})=0,0,MAX(0,N(T{r})-W{r})))')
        ws_orders.cell(r, 25, f'=IF(A{r}="","",IF(AND(N(T{r})>0,W{r}>=N(T{r})),"Payment Complete",IF(AND(N(T{r})>0,W{r}>0.5*N(T{r})),"Partial / Pre-Dispatch",IF(W{r}>0,"Advance In","Advance Pending"))))')
        ws_orders.cell(r, 27, f'=IF(A{r}="","",N(Z{r})+SUMIFS(\'6_Money_Ledger\'!$H$6:$H$500,\'6_Money_Ledger\'!$E$6:$E$500,A{r},\'6_Money_Ledger\'!$B$6:$B$500,"Expense")+SUMIFS(\'6_Money_Ledger\'!$H$6:$H$500,\'6_Money_Ledger\'!$E$6:$E$500,A{r},\'6_Money_Ledger\'!$B$6:$B$500,"Vendor Payment")+SUMIFS(\'6_Money_Ledger\'!$H$6:$H$500,\'6_Money_Ledger\'!$E$6:$E$500,A{r},\'6_Money_Ledger\'!$B$6:$B$500,"Supplier Bill (Draft)"))')
        ws_orders.cell(r, 28, f'=IF(A{r}="","",IF(N(T{r})=0,W{r}-AA{r},N(T{r})-AA{r}))')

        for col_i in range(1, 30):
            cell = ws_orders.cell(r, col_i)
            cell.font = FONT_BOLD if col_i in (1, 3, 24) else FONT_BODY
            cell.fill = fill
            cell.border = CELL_BORDER
            if col_i in (20, 21, 22, 23, 24, 26, 27, 28):
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif col_i in (12, 14):
                cell.number_format = DATE_FMT
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_i in (2, 11, 13, 17, 18, 19, 25):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=(col_i in (6, 29)))

    _add_list_dv(ws_orders, R_ORDER_STATUS, f"B6:B{max_order_row}")
    _add_list_dv(ws_orders, R_CUSTOMERS, f"C6:C{max_order_row}")
    _add_list_dv(ws_orders, R_PIPE_STAGES, f"G6:G{max_order_row}")
    _add_list_dv(ws_orders, R_PEOPLE, f"H6:J{max_order_row}")
    _add_list_dv(ws_orders, R_YESNO, f"K6:K{max_order_row}")
    _add_list_dv(ws_orders, R_MAT_TRACK, f"O6:O{max_order_row}")
    _add_list_dv(ws_orders, R_PEOPLE, f"P6:P{max_order_row}")

    _apply_pill_cf(ws_orders, f"G6:G{max_order_row}", [
        ("Completed", C_GREEN_BG, C_GREEN_FG),
        ("Execution", C_BLUE_BG, C_BLUE_FG),
        ("Site Sizing / BOQ", C_AMBER_BG, C_AMBER_FG),
        ("Quoted to Customer / Awaiting Advance", C_AMBER_BG, C_AMBER_FG),
        ("On Hold", C_ROSE_BG, C_ROSE_FG),
        ("Lost / Dropped", C_GRAY_BG, C_GRAY_FG),
    ])
    _apply_pill_cf(ws_orders, f"Y6:Y{max_order_row}", [
        ("Payment Complete", C_GREEN_BG, C_GREEN_FG),
        ("Partial / Pre-Dispatch", C_BLUE_BG, C_BLUE_FG),
        ("Advance In", C_AMBER_BG, C_AMBER_FG),
        ("Advance Pending", C_ROSE_BG, C_ROSE_FG),
    ])
    _apply_pill_cf(ws_orders, f"O6:O{max_order_row}", [
        ("All In Shop", C_GREEN_BG, C_GREEN_FG),
        ("Ordered (Pending)", C_AMBER_BG, C_AMBER_FG),
        ("Check Stock", C_ROSE_BG, C_ROSE_FG),
    ])
    ws_orders.conditional_formatting.add(
        f"M6:M{max_order_row}",
        CellIsRule(operator="greaterThanOrEqual", formula=["5"], fill=PatternFill("solid", fgColor=C_ROSE_BG), font=Font(name="Calibri", size=10, bold=True, color=C_ROSE_FG)),
    )
    ws_orders.conditional_formatting.add(
        f"M6:M{max_order_row}",
        CellIsRule(operator="between", formula=["3", "4"], fill=PatternFill("solid", fgColor=C_AMBER_BG), font=Font(name="Calibri", size=10, bold=True, color=C_AMBER_FG)),
    )

    ws_orders.auto_filter.ref = f"A5:AC{max_order_row}"
    ws_orders.freeze_panes = "D6"
    widths_orders = [
        14, 11, 22, 22, 14, 28,
        28, 18, 16, 16, 12, 13, 12, 14,
        18, 15, 11, 11, 15,
        16, 15, 15, 16, 16, 20, 15, 15, 15, 28,
    ]
    for col_i, w in enumerate(widths_orders, 1):
        ws_orders.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_orders)

    # =========================================================================
    # TAB 3: 3_Furniture_Items (Tier 2 Single-Status Item Execution)
    # =========================================================================
    _style_banner(
        ws_items,
        15,
        "Furniture Items Board — Single-Status Stage Tracking, Permanent Shop Mistri & Dispatch Gate",
        "Pick the Item Stage via dropdown ('In Shop' -> 'Packed / Ready' -> 'At Site' -> 'QC Done'). Keep Shop Mistri filled permanently even after moving to site.",
    )
    item_headers = [
        "Order Code", "Customer Name (Auto)", "Furniture Item", "Category",
        "Dimensions", "Specs / Finish Notes", "Item Stage", "Shop Mistri (Permanent)",
        "Site Assembly Mistri", "QC Passed By", "Stage Date", "Days in Stage",
        "Target Date", "Materials Pending", "Pre-Dispatch Payment Check", "JobCardKey",
    ]
    ws_items.row_dimensions[5].height = 28
    for idx, h in enumerate(item_headers, 1):
        c = ws_items.cell(5, idx, h)
        c.font = FONT_TH
        c.fill = FILL_TH
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = CELL_BORDER

    items_sorted = sorted(
        items,
        key=lambda it: (
            str((proj_by_id.get(str(it.get("project_id") or "")) or {}).get("project_code") or ""),
            str(it.get("item_name") or ""),
        ),
    )
    max_item_row = 300
    for i in range(max_item_row - 5):
        r = 6 + i
        ws_items.row_dimensions[r].height = 23
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        it = items_sorted[i] if i < len(items_sorted) else None

        if it:
            prj = proj_by_id.get(str(it.get("project_id") or "")) or {}
            ws_items.cell(r, 1, prj.get("project_code") or "")
            ws_items.cell(r, 3, it.get("item_name") or "")
            ws_items.cell(r, 4, it.get("category") or "")
            ws_items.cell(r, 5, it.get("dimensions") or "")
            ws_items.cell(r, 6, it.get("specs_notes") or "")
            ws_items.cell(r, 7, item_stage_map.get(it.get("stage") or "in_shop", "In Shop"))
            ws_items.cell(r, 8, p_name(it.get("production_mistri_id")))
            ws_items.cell(r, 9, p_name(it.get("assembly_mistri_id")))
            ws_items.cell(r, 10, p_name(it.get("qc_passed_by_id")))
            ws_items.cell(r, 11, _parse_date(it.get("stage_entered_at")) or _parse_date(it.get("created_at")))
            ws_items.cell(r, 13, _parse_date(it.get("target_date")))

        ws_items.cell(r, 2, f'=IF(A{r}="","",IFERROR(INDEX(\'2_Orders_Master\'!$C$6:$C$150,MATCH(A{r},\'2_Orders_Master\'!$A$6:$A$150,0)),""))')
        ws_items.cell(r, 12, f'=IF(OR(C{r}="",G{r}="QC Done",K{r}=""),0,MAX(0,TODAY()-K{r}))')
        ws_items.cell(r, 14, f'=IF(OR(A{r}="",C{r}=""),"",COUNTIFS(\'4_Raw_Materials\'!$A$6:$A$400,A{r},\'4_Raw_Materials\'!$C$6:$C$400,C{r},\'4_Raw_Materials\'!$H$6:$H$400,"<>In Shop"))')
        ws_items.cell(r, 15, f'=IF(OR(A{r}="",C{r}=""),"",IF(G{r}="Packed / Ready",IF(IFERROR(INDEX(\'2_Orders_Master\'!$Y$6:$Y$150,MATCH(A{r},\'2_Orders_Master\'!$A$6:$A$150,0)),"")="Advance Pending","Collect Pre-Dispatch Balance","Ready for Site Dispatch"),""))')
        ws_items.cell(r, 16, f'=IF(A{r}="","",A{r}&"-"&COUNTIF($A$6:A{r},A{r}))')

        for col_i in range(1, 17):
            cell = ws_items.cell(r, col_i)
            cell.font = FONT_BOLD if col_i in (1, 3, 7) else FONT_BODY
            cell.fill = fill
            cell.border = CELL_BORDER
            if col_i in (11, 13):
                cell.number_format = DATE_FMT
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col_i in (7, 12, 14, 15):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=(col_i == 6))

    ws_items.column_dimensions["P"].hidden = True
    _add_list_dv(ws_items, R_ORDERS, f"A6:A{max_item_row}")
    _add_list_dv(ws_items, R_ITEM_STAGES, f"G6:G{max_item_row}")
    _add_list_dv(ws_items, R_PEOPLE, f"H6:J{max_item_row}")

    _apply_pill_cf(ws_items, f"G6:G{max_item_row}", [
        ("In Shop", C_BLUE_BG, C_BLUE_FG),
        ("Packed / Ready", C_AMBER_BG, C_AMBER_FG),
        ("At Site", "E0E7FF", "3730A3"),
        ("QC Done", C_GREEN_BG, C_GREEN_FG),
    ])
    _apply_pill_cf(ws_items, f"O6:O{max_item_row}", [
        ("Collect Pre-Dispatch Balance", C_ROSE_BG, C_ROSE_FG),
        ("Ready for Site Dispatch", C_GREEN_BG, C_GREEN_FG),
    ])

    ws_items.auto_filter.ref = f"A5:O{max_item_row}"
    ws_items.freeze_panes = "D6"
    widths_items = [14, 22, 24, 15, 16, 26, 16, 20, 20, 16, 13, 12, 13, 14, 24, 14]
    for col_i, w in enumerate(widths_items, 1):
        ws_items.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_items)

    # =========================================================================
    # TAB 4: 4_Raw_Materials (Per-Furniture & Customer-Level Raw Materials)
    # =========================================================================
    _style_banner(
        ws_mats,
        10,
        "Raw Materials Tracker — Per-Furniture Requirements & Customer Filter View",
        "Add raw materials linked to an Order Code + Furniture Item. Filter Col B (Customer Name) for full customer visibility, or filter Col H = 'To Order' for Ansh's purchase list.",
    )
    # Row 3 live filter bar
    ws_mats.row_dimensions[3].height = 22
    summary_specs = [
        (1, "Visible Filtered Rows:", '=SUBTOTAL(103,E6:E400)', FILL_CREAM),
        (3, "Total To Order:", '=COUNTIFS(H6:H400,"To Order")', C_ROSE_BG),
        (5, "Total Ordered:", '=COUNTIFS(H6:H400,"Ordered")', C_AMBER_BG),
        (7, "Total In Shop:", '=COUNTIFS(H6:H400,"In Shop")', C_GREEN_BG),
    ]
    for col_s, lbl, fml, bg in summary_specs:
        lc = ws_mats.cell(3, col_s, lbl)
        lc.font = FONT_BOLD
        lc.alignment = Alignment(horizontal="right", vertical="center")
        vc = ws_mats.cell(3, col_s + 1, fml)
        vc.font = FONT_BOLD
        vc.fill = bg if isinstance(bg, PatternFill) else PatternFill("solid", fgColor=bg)
        vc.alignment = Alignment(horizontal="center", vertical="center")
        vc.border = CELL_BORDER

    mat_headers = [
        "Order Code", "Customer Name (Auto)", "Furniture Item", "Category",
        "Material / Brand / Spec", "Qty", "Unit", "Material Status",
        "Preferred Supplier", "Notes",
    ]
    ws_mats.row_dimensions[5].height = 26
    for idx, h in enumerate(mat_headers, 1):
        c = ws_mats.cell(5, idx, h)
        c.font = FONT_TH
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = CELL_BORDER

    max_mat_row = 400
    for i in range(max_mat_row - 5):
        r = 6 + i
        ws_mats.row_dimensions[r].height = 22
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        m = materials[i] if i < len(materials) else None

        if m:
            prj = proj_by_id.get(str(m.get("project_id") or "")) or {}
            it = item_by_id.get(str(m.get("item_id") or "")) or {}
            ws_mats.cell(r, 1, prj.get("project_code") or "")
            ws_mats.cell(r, 3, it.get("item_name") or "")
            ws_mats.cell(r, 4, mat_cat_map.get(m.get("category") or "", m.get("category") or ""))
            ws_mats.cell(r, 5, m.get("material_name") or "")
            ws_mats.cell(r, 6, float(m.get("quantity") or 0))
            ws_mats.cell(r, 7, m.get("unit") or "pcs")
            ws_mats.cell(r, 8, mat_status_map.get(m.get("status") or "needed", "To Order"))
            ws_mats.cell(r, 9, m.get("preferred_supplier") or "")
            ws_mats.cell(r, 10, m.get("notes") or "")

        ws_mats.cell(r, 2, f'=IF(A{r}="","",IFERROR(INDEX(\'2_Orders_Master\'!$C$6:$C$150,MATCH(A{r},\'2_Orders_Master\'!$A$6:$A$150,0)),""))')

        for col_i in range(1, 11):
            cell = ws_mats.cell(r, col_i)
            cell.font = FONT_BOLD if col_i in (1, 5, 8) else FONT_BODY
            cell.fill = fill
            cell.border = CELL_BORDER
            if col_i == 6:
                cell.number_format = QTY_FMT
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif col_i in (7, 8):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    _add_list_dv(ws_mats, R_ORDERS, f"A6:A{max_mat_row}")
    _add_list_dv(ws_mats, R_MAT_CATS, f"D6:D{max_mat_row}")
    _add_list_dv(ws_mats, R_UNITS, f"G6:G{max_mat_row}")
    _add_list_dv(ws_mats, R_MAT_STATUS, f"H6:H{max_mat_row}")
    _add_list_dv(ws_mats, R_VENDORS, f"I6:I{max_mat_row}")
    _apply_pill_cf(ws_mats, f"H6:H{max_mat_row}", [
        ("To Order", C_ROSE_BG, C_ROSE_FG),
        ("Ordered", C_AMBER_BG, C_AMBER_FG),
        ("In Shop", C_GREEN_BG, C_GREEN_FG),
    ])

    ws_mats.auto_filter.ref = f"A5:J{max_mat_row}"
    ws_mats.freeze_panes = "D6"
    widths_mats = [14, 22, 22, 20, 30, 10, 10, 14, 22, 26]
    for col_i, w in enumerate(widths_mats, 1):
        ws_mats.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_mats)

    # =========================================================================
    # TAB 5: 5_Karigar_Job_Card (Price-Free Printable Karigar Card)
    # =========================================================================
    _style_banner(
        ws_job,
        7,
        "THE FURNITURE COMPANY — KARIGAR JOB CARD (PRICE-FREE)",
        "Select any Order Code in cell B3 below to generate a clean, zero-price Karigar work sheet ready to print or screenshot on WhatsApp.",
    )
    default_order_code = ""
    if items_sorted:
        first_prj = proj_by_id.get(str(items_sorted[0].get("project_id") or ""))
        default_order_code = str((first_prj or {}).get("project_code") or "")
    elif sorted_projects:
        default_order_code = str(sorted_projects[0].get("project_code") or "")

    meta_rows = [
        ("Select Order Code:", default_order_code, "Customer Name:", '=IF(B3="","",IFERROR(INDEX(\'2_Orders_Master\'!$C$6:$C$150,MATCH($B$3,\'2_Orders_Master\'!$A$6:$A$150,0)),""))'),
        ("Site / Landmark:", '=IF(B3="","",IFERROR(INDEX(\'2_Orders_Master\'!$D$6:$D$150,MATCH($B$3,\'2_Orders_Master\'!$A$6:$A$150,0)),""))', "Promised Delivery:", '=IF(B3="","",IFERROR(INDEX(\'2_Orders_Master\'!$N$6:$N$150,MATCH($B$3,\'2_Orders_Master\'!$A$6:$A$150,0)),""))'),
        ("Overall Scope:", '=IF(B3="","",IFERROR(INDEX(\'2_Orders_Master\'!$F$6:$F$150,MATCH($B$3,\'2_Orders_Master\'!$A$6:$A$150,0)),""))', "Sizing Mistri:", '=IF(B3="","",IFERROR(INDEX(\'2_Orders_Master\'!$J$6:$J$150,MATCH($B$3,\'2_Orders_Master\'!$A$6:$A$150,0)),""))'),
    ]
    for offset, (l1, v1, l2, v2) in enumerate(meta_rows):
        r = 3 + offset
        ws_job.row_dimensions[r].height = 24
        c_l1 = ws_job.cell(r, 1, l1)
        c_l1.font = FONT_BOLD
        c_l1.fill = FILL_CREAM
        c_l1.border = CELL_BORDER

        c_v1 = ws_job.cell(r, 2, v1)
        c_v1.font = Font(name="Calibri", size=11, bold=True, color=C_ESPRESSO)
        c_v1.fill = FILL_SOFT if (r == 3) else FILL_WHITE
        c_v1.border = Border(left=MED_AMBER, right=MED_AMBER, top=MED_AMBER, bottom=MED_AMBER) if r == 3 else CELL_BORDER

        ws_job.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
        c_l2 = ws_job.cell(r, 3, l2)
        c_l2.font = FONT_BOLD
        c_l2.fill = FILL_CREAM
        c_l2.alignment = Alignment(horizontal="right", vertical="center")
        c_l2.border = CELL_BORDER
        ws_job.cell(r, 4).border = CELL_BORDER

        ws_job.merge_cells(start_row=r, start_column=5, end_row=r, end_column=7)
        c_v2 = ws_job.cell(r, 5, v2)
        c_v2.font = FONT_BOLD
        c_v2.border = CELL_BORDER
        if r == 4:
            c_v2.number_format = DATE_FMT
        for cc in (6, 7):
            ws_job.cell(r, cc).border = CELL_BORDER

    _add_list_dv(ws_job, R_ORDERS, "B3")
    job_headers = ["#", "Furniture Item", "Category", "Dimensions", "Specs / Work Notes", "Shop Mistri", "Current Stage"]
    ws_job.row_dimensions[7].height = 26
    for idx, h in enumerate(job_headers, 1):
        c = ws_job.cell(7, idx, h)
        c.font = FONT_TH
        c.fill = PatternFill("solid", fgColor="1E40AF")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = CELL_BORDER

    for slot in range(1, 21):
        r = 7 + slot
        ws_job.row_dimensions[r].height = 24
        fill = FILL_STRIPE if slot % 2 == 0 else FILL_WHITE
        key_expr = f"$B$3&\"-\"&A{r}"
        ws_job.cell(r, 1, slot)
        ws_job.cell(r, 2, f'=IFERROR(INDEX(\'3_Furniture_Items\'!$C$6:$C$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),"")')
        ws_job.cell(r, 3, f'=IFERROR(INDEX(\'3_Furniture_Items\'!$D$6:$D$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),"")')
        ws_job.cell(r, 4, f'=IFERROR(INDEX(\'3_Furniture_Items\'!$E$6:$E$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),"")')
        ws_job.cell(r, 5, f'=IFERROR(INDEX(\'3_Furniture_Items\'!$F$6:$F$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),"")')
        ws_job.cell(r, 6, f'=IFERROR(INDEX(\'3_Furniture_Items\'!$H$6:$H$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),"")')
        ws_job.cell(r, 7, f'=IF(B{r}="","",IFERROR(INDEX(\'3_Furniture_Items\'!$G$6:$G$300,MATCH({key_expr},\'3_Furniture_Items\'!$P$6:$P$300,0)),""))')
        for col_i in range(1, 8):
            c = ws_job.cell(r, col_i)
            c.font = FONT_BOLD if col_i in (1, 2, 7) else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            c.alignment = Alignment(horizontal="center" if col_i in (1, 7) else "left", vertical="center", wrap_text=(col_i == 5))

    for col_i, w in enumerate([18, 26, 16, 18, 34, 20, 16], 1):
        ws_job.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_job)

    # =========================================================================
    # TAB 6: 6_Money_Ledger (Munim Cashbook: Banks, Cash Galla & 4 Cash Wallets)
    # =========================================================================
    _style_banner(
        ws_ledger,
        11,
        "Munim Money Ledger — Receipts, Expenses, Vendor Payouts, 4 Wallet Transfers & Draft Bills",
        "Log daily money movements here. Selecting an Order Code auto-fills the Customer Name and updates 2_Orders_Master (Received / Pending / Margin) and 1_Dashboard_Queues balances.",
    )
    ledger_headers = [
        "Date", "Txn Type", "Account / Wallet", "Transfer To (If Wallet Transfer)",
        "Order Code", "Customer Name (Auto)", "Party / Vendor / Item",
        "Amount (₹)", "Bill / Ref No", "Entered By", "Notes",
    ]
    ws_ledger.row_dimensions[5].height = 28
    for idx, h in enumerate(ledger_headers, 1):
        c = ws_ledger.cell(5, idx, h)
        c.font = FONT_TH
        c.fill = PatternFill("solid", fgColor="166534")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = CELL_BORDER

    max_ledger_row = 500
    for i in range(max_ledger_row - 5):
        r = 6 + i
        ws_ledger.row_dimensions[r].height = 22
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        ws_ledger.cell(r, 6, f'=IF(E{r}="","",IFERROR(INDEX(\'2_Orders_Master\'!$C$6:$C$150,MATCH(E{r},\'2_Orders_Master\'!$A$6:$A$150,0)),""))')
        for col_i in range(1, 12):
            c = ws_ledger.cell(r, col_i)
            c.font = FONT_BOLD if col_i in (2, 8) else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            if col_i == 1:
                c.number_format = DATE_FMT
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif col_i == 8:
                c.number_format = INR_FMT
                c.alignment = Alignment(horizontal="right", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

    _add_list_dv(ws_ledger, R_TXN_TYPES, f"B6:B{max_ledger_row}")
    _add_list_dv(ws_ledger, R_ACCOUNTS, f"C6:D{max_ledger_row}")
    _add_list_dv(ws_ledger, R_ORDERS, f"E6:E{max_ledger_row}")
    _add_list_dv(ws_ledger, R_PEOPLE, f"J6:J{max_ledger_row}")
    _apply_pill_cf(ws_ledger, f"B6:B{max_ledger_row}", [
        ("Customer Receipt", C_GREEN_BG, C_GREEN_FG),
        ("Wallet Transfer", C_BLUE_BG, C_BLUE_FG),
        ("Supplier Bill (Draft)", C_AMBER_BG, C_AMBER_FG),
        ("Expense", C_ROSE_BG, C_ROSE_FG),
        ("Vendor Payment", C_ROSE_BG, C_ROSE_FG),
    ])

    ws_ledger.auto_filter.ref = f"A5:K{max_ledger_row}"
    ws_ledger.freeze_panes = "C6"
    for col_i, w in enumerate([13, 21, 22, 22, 14, 22, 24, 16, 15, 16, 26], 1):
        ws_ledger.column_dimensions[get_column_letter(col_i)].width = w
    _setup_print(ws_ledger)

    # =========================================================================
    # TAB 1: 1_Dashboard_Queues (Control Tower: KPIs, Accounts Pulse, Queues, Mistris)
    # =========================================================================
    _style_banner(
        ws_dash,
        8,
        "WORKSHOP MUNIM OS — SHREYANSH LIVE CONTROL TOWER",
        f"Snapshot synced {datetime.now():%d-%b-%Y %H:%M}. KPIs, Account/Wallet Balances, Office Queues, and Mistri Load recalculate live as you edit tabs 2–6.",
    )

    kpis = [
        ("Active Orders", '=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$A$6:$A$150,"<>GEN-STOCK")', "0"),
        ("In Execution", '=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$G$6:$G$150,"Execution",\'2_Orders_Master\'!$A$6:$A$150,"<>GEN-STOCK")', "0"),
        ("Stale Orders (>=3d)", '=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$M$6:$M$150,">=3",\'2_Orders_Master\'!$A$6:$A$150,"<>GEN-STOCK")', "0"),
        ("Packed / QC Queue", '=COUNTIFS(\'3_Furniture_Items\'!$G$6:$G$300,"Packed / Ready")', "0"),
        ("Mats To Order", '=COUNTIFS(\'4_Raw_Materials\'!$H$6:$H$400,"To Order")', "0"),
        ("Active Deal Value", '=SUMIFS(\'2_Orders_Master\'!$T$6:$T$150,\'2_Orders_Master\'!$B$6:$B$150,"Active")', INR_FMT),
        ("Total Received", '=SUMIFS(\'2_Orders_Master\'!$W$6:$W$150,\'2_Orders_Master\'!$A$6:$A$150,"<>GEN-STOCK")', INR_FMT),
        ("Pending Receivables", '=SUMIFS(\'2_Orders_Master\'!$X$6:$X$150,\'2_Orders_Master\'!$B$6:$B$150,"Active")', INR_FMT),
    ]
    ws_dash.row_dimensions[4].height = 20
    ws_dash.row_dimensions[5].height = 28
    for idx, (lbl, fml, fmt) in enumerate(kpis, 1):
        lc = ws_dash.cell(4, idx, lbl)
        lc.font = FONT_KPI_LBL
        lc.fill = FILL_SOFT
        lc.alignment = Alignment(horizontal="center", vertical="center")
        lc.border = Border(left=THIN_SIDE, right=THIN_SIDE, top=MED_AMBER)

        vc = ws_dash.cell(5, idx, fml)
        vc.font = FONT_KPI_VAL
        vc.fill = FILL_CREAM
        vc.number_format = fmt
        vc.alignment = Alignment(horizontal="center", vertical="center")
        vc.border = Border(left=THIN_SIDE, right=THIN_SIDE, bottom=MED_AMBER)

    # Section A: Pipeline Stage Breakdown (Rows 7..17, Cols A:C) & Cash/Bank/Wallets Pulse (Cols A:F below — wait let's stack cleanly A:H!)
    # Section 1: Owner Pulse — Cash Galla, Banks & 4 Cash Wallets
    r_cur = 7
    ws_dash.merge_cells(start_row=r_cur, start_column=1, end_row=r_cur, end_column=6)
    sc = ws_dash.cell(r_cur, 1, "OWNER PULSE — CASH GALLA, BANKS & 4 CASH WALLETS (₹)")
    sc.font = FONT_SEC
    sc.fill = PatternFill("solid", fgColor="166534")
    sc.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws_dash.row_dimensions[r_cur].height = 22

    # Right beside it in G7:H7 -> Pipeline Breakdown Header
    ws_dash.merge_cells(start_row=r_cur, start_column=7, end_row=r_cur, end_column=8)
    sp = ws_dash.cell(r_cur, 7, "ACTIVE ORDERS BY PIPELINE STAGE")
    sp.font = FONT_SEC
    sp.fill = FILL_SEC
    sp.alignment = Alignment(horizontal="left", vertical="center", indent=1)

    r_hdr = r_cur + 1
    ws_dash.row_dimensions[r_hdr].height = 24
    acc_hdrs = ["Account / Wallet", "Kind", "Zoho Link", "Opening (₹)", "Ledger Net (₹)", "Closing Bal (₹)"]
    for ci, h in enumerate(acc_hdrs, 1):
        c = ws_dash.cell(r_hdr, ci, h)
        c.font = FONT_TH
        c.fill = PatternFill("solid", fgColor="166534")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = CELL_BORDER
    for ci, h in [(7, "Pipeline Stage"), (8, "Orders")]:
        c = ws_dash.cell(r_hdr, ci, h)
        c.font = FONT_TH
        c.fill = FILL_TH
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = CELL_BORDER

    kind_labels = {
        "cash_galla": "Cash Galla",
        "bank": "Bank",
        "od_cc": "OD / CC",
        "wallet": "UPI / Wallet",
        "supervisor_float": "Cash Wallet",
    }
    acc_start = r_hdr + 1
    for i, a in enumerate(accounts):
        r = acc_start + i
        ws_dash.row_dimensions[r].height = 21
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        ws_dash.cell(r, 1, a["display_name"])
        ws_dash.cell(r, 2, kind_labels.get(a.get("account_kind") or "", ""))
        ws_dash.cell(r, 3, "Synced" if a.get("zoho_account_id") else "Add in Zoho")
        ws_dash.cell(r, 4, round(float(a.get("cached_balance_inr") or 0), 2))
        in_f = (
            f"SUMIFS('6_Money_Ledger'!$H$6:$H$500,'6_Money_Ledger'!$C$6:$C$500,A{r},'6_Money_Ledger'!$B$6:$B$500,\"Customer Receipt\")"
            f"+SUMIFS('6_Money_Ledger'!$H$6:$H$500,'6_Money_Ledger'!$D$6:$D$500,A{r},'6_Money_Ledger'!$B$6:$B$500,\"Wallet Transfer\")"
        )
        out_f = (
            f"SUMIFS('6_Money_Ledger'!$H$6:$H$500,'6_Money_Ledger'!$C$6:$C$500,A{r},'6_Money_Ledger'!$B$6:$B$500,\"Expense\")"
            f"+SUMIFS('6_Money_Ledger'!$H$6:$H$500,'6_Money_Ledger'!$C$6:$C$500,A{r},'6_Money_Ledger'!$B$6:$B$500,\"Vendor Payment\")"
            f"+SUMIFS('6_Money_Ledger'!$H$6:$H$500,'6_Money_Ledger'!$C$6:$C$500,A{r},'6_Money_Ledger'!$B$6:$B$500,\"Wallet Transfer\")"
        )
        ws_dash.cell(r, 5, f"=({in_f})-({out_f})")
        ws_dash.cell(r, 6, f"=D{r}+E{r}")
        for ci in range(1, 7):
            c = ws_dash.cell(r, ci)
            c.font = FONT_BOLD if ci in (1, 6) else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            if ci in (4, 5, 6):
                c.number_format = INR_FMT
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif ci in (2, 3):
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

    acc_end = acc_start + len(accounts) - 1
    r_acc_tot = acc_end + 1
    ws_dash.row_dimensions[r_acc_tot].height = 22
    ws_dash.cell(r_acc_tot, 1, "Total Cash, Banks & Wallets")
    ws_dash.cell(r_acc_tot, 4, f"=SUM(D{acc_start}:D{acc_end})")
    ws_dash.cell(r_acc_tot, 5, f"=SUM(E{acc_start}:E{acc_end})")
    ws_dash.cell(r_acc_tot, 6, f"=SUM(F{acc_start}:F{acc_end})")
    for ci in range(1, 7):
        c = ws_dash.cell(r_acc_tot, ci)
        c.font = FONT_BOLD
        c.fill = FILL_TOTAL
        c.border = TOTAL_BORDER
        if ci in (4, 5, 6):
            c.number_format = INR_FMT
            c.alignment = Alignment(horizontal="right", vertical="center")

    # Populate right-side Pipeline Breakdown rows matching rows acc_start..
    for i, stage_lbl in enumerate(pipeline_labels):
        r = acc_start + i
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        c_st = ws_dash.cell(r, 7, stage_lbl)
        c_st.font = FONT_BODY
        c_st.fill = fill
        c_st.border = CELL_BORDER
        c_st.alignment = Alignment(horizontal="left", vertical="center")
        c_cnt = ws_dash.cell(r, 8, f'=COUNTIFS(\'2_Orders_Master\'!$G$6:$G$150,G{r},\'2_Orders_Master\'!$A$6:$A$150,"<>GEN-STOCK")')
        c_cnt.font = FONT_BOLD
        c_cnt.fill = fill
        c_cnt.border = CELL_BORDER
        c_cnt.alignment = Alignment(horizontal="center", vertical="center")

    # Section 2: Office Team Queues (Who's Doing What)
    r_team = max(r_acc_tot, acc_start + len(pipeline_labels) - 1) + 2
    ws_dash.merge_cells(start_row=r_team, start_column=1, end_row=r_team, end_column=6)
    st_c = ws_dash.cell(r_team, 1, "WHO'S DOING WHAT — OFFICE TEAM QUEUES")
    st_c.font = FONT_SEC
    st_c.fill = FILL_SEC
    st_c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws_dash.row_dimensions[r_team].height = 22

    r_team_hdr = r_team + 1
    ws_dash.row_dimensions[r_team_hdr].height = 24
    team_hdrs = ["Operator", "Role Focus", "Orders Owned", "Estimating Queue", "Material Track", "Pre-Pack QC"]
    for ci, h in enumerate(team_hdrs, 1):
        c = ws_dash.cell(r_team_hdr, ci, h)
        c.font = FONT_TH
        c.fill = FILL_TH
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = CELL_BORDER

    operators = [p for p in people if p.get("person_type") == "operator" and p.get("is_active", True)]
    for i, op in enumerate(operators):
        r = r_team_hdr + 1 + i
        ws_dash.row_dimensions[r].height = 21
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        ws_dash.cell(r, 1, op["full_name"])
        ws_dash.cell(r, 2, ", ".join(op.get("role_tags") or []))
        ws_dash.cell(r, 3, f'=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$H$6:$H$150,"*"&A{r}&"*")')
        ws_dash.cell(r, 4, f'=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$G$6:$G$150,"Estimating",\'2_Orders_Master\'!$I$6:$I$150,"*"&A{r}&"*")')
        ws_dash.cell(r, 5, f'=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$P$6:$P$150,A{r},\'2_Orders_Master\'!$O$6:$O$150,"<>All In Shop")')
        if op.get("is_default_qc"):
            ws_dash.cell(r, 6, '=COUNTIFS(\'3_Furniture_Items\'!$G$6:$G$300,"Packed / Ready")')
        else:
            ws_dash.cell(r, 6, 0)
        for ci in range(1, 7):
            c = ws_dash.cell(r, ci)
            c.font = FONT_BOLD if ci == 1 else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            c.alignment = Alignment(horizontal="center" if ci >= 3 else "left", vertical="center")

    # Section 3: Mistri Floor & Site Load
    r_mis = r_team_hdr + len(operators) + 2
    ws_dash.merge_cells(start_row=r_mis, start_column=1, end_row=r_mis, end_column=6)
    sm_c = ws_dash.cell(r_mis, 1, "MISTRI FLOOR & SITE LOAD (IN SHOP / AT SITE / SITE SIZING)")
    sm_c.font = FONT_SEC
    sm_c.fill = PatternFill("solid", fgColor=C_AMBER_MID)
    sm_c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws_dash.row_dimensions[r_mis].height = 22

    r_mis_hdr = r_mis + 1
    ws_dash.row_dimensions[r_mis_hdr].height = 24
    mis_hdrs = ["Mistri / Karigar", "Type", "In Shop Items", "At Site Items", "Site Sizing Orders", "Total Active Load"]
    for ci, h in enumerate(mis_hdrs, 1):
        c = ws_dash.cell(r_mis_hdr, ci, h)
        c.font = FONT_TH
        c.fill = FILL_TH_ALT
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = CELL_BORDER

    mistris = [
        p for p in people
        if p.get("person_type") in ("salaried_karigar", "contractor_mistri") and p.get("is_active", True)
    ]
    for i, m_p in enumerate(mistris):
        r = r_mis_hdr + 1 + i
        ws_dash.row_dimensions[r].height = 21
        fill = FILL_STRIPE if i % 2 else FILL_WHITE
        ws_dash.cell(r, 1, m_p["full_name"])
        ws_dash.cell(r, 2, "Salaried" if m_p.get("person_type") == "salaried_karigar" else "Contractor")
        ws_dash.cell(r, 3, f'=COUNTIFS(\'3_Furniture_Items\'!$G$6:$G$300,"In Shop",\'3_Furniture_Items\'!$H$6:$H$300,A{r})')
        ws_dash.cell(r, 4, f'=COUNTIFS(\'3_Furniture_Items\'!$G$6:$G$300,"At Site",\'3_Furniture_Items\'!$I$6:$I$300,A{r})')
        ws_dash.cell(r, 5, f'=COUNTIFS(\'2_Orders_Master\'!$B$6:$B$150,"Active",\'2_Orders_Master\'!$G$6:$G$150,"Site Sizing / BOQ",\'2_Orders_Master\'!$J$6:$J$150,A{r})')
        ws_dash.cell(r, 6, f"=SUM(C{r}:E{r})")
        for ci in range(1, 7):
            c = ws_dash.cell(r, ci)
            c.font = FONT_BOLD if ci in (1, 6) else FONT_BODY
            c.fill = fill
            c.border = CELL_BORDER
            c.alignment = Alignment(horizontal="center" if ci >= 2 else "left", vertical="center")

    _apply_pill_cf(ws_dash, f"C{acc_start}:C{acc_end}", [
        ("Synced", C_GREEN_BG, C_GREEN_FG),
        ("Add in Zoho", C_AMBER_BG, C_AMBER_FG),
    ])
    for ci, w in enumerate([25, 22, 16, 17, 17, 18, 34, 15], 1):
        ws_dash.column_dimensions[get_column_letter(col_i := ci)].width = w
    _setup_print(ws_dash)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    out_path = build_workbook()
    print(f"Generated workbook: {out_path}")
