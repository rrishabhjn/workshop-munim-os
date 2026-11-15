import csv
from datetime import datetime, timezone
from io import StringIO
import re
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user
from app.db import get_supabase_admin
from app.services import storage

router = APIRouter(prefix="/api", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------
class StageCreate(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    code: str | None = None
    sort_order: int | None = None


class StageUpdate(BaseModel):
    label: str | None = None
    sort_order: int | None = None


class StageReorder(BaseModel):
    codes: list[str]


class ProjectCreate(BaseModel):
    project_code: str
    customer_name: str
    phone: str | None = None
    quoted_total: float = 0.0
    gst_flag: bool = True
    promised_delivery_date: str | None = None


class ProjectUpdate(BaseModel):
    customer_name: str | None = None
    phone: str | None = None
    quoted_total: float | None = None
    gst_flag: bool | None = None
    promised_delivery_date: str | None = None
    status: str | None = None


class ItemCreate(BaseModel):
    item_name: str
    category: str | None = None
    dimensions: str | None = None
    specs_notes: str | None = None
    stage: str = "design_measurement"
    assigned_karigar: str | None = None
    target_date: str | None = None


class ItemUpdate(BaseModel):
    item_name: str | None = None
    category: str | None = None
    dimensions: str | None = None
    specs_notes: str | None = None
    stage: str | None = None
    assigned_karigar: str | None = None
    target_date: str | None = None


class PartCreate(BaseModel):
    part_name: str
    stage: str = "design_measurement"
    assigned_karigar: str | None = None
    is_done: bool = False
    sort_order: int = 0


class PartUpdate(BaseModel):
    part_name: str | None = None
    stage: str | None = None
    assigned_karigar: str | None = None
    is_done: bool | None = None
    sort_order: int | None = None


class MaterialCreate(BaseModel):
    category: str = "ply_wood"
    material_name: str
    quantity: float = 1.0
    unit: str = "pcs"
    status: str = "needed"
    preferred_supplier: str | None = None
    notes: str | None = None


class MaterialUpdate(BaseModel):
    status: str | None = None
    quantity: float | None = None
    preferred_supplier: str | None = None
    notes: str | None = None


class CashLedgerCreate(BaseModel):
    txn_date: str | None = None
    txn_type: str
    direction: str
    party_or_karigar: str
    project_id: str | None = None
    item_id: str | None = None
    amount_inr: float = Field(ge=0)
    payment_mode: str = "cash"
    notes: str | None = None


class CashLedgerUpdate(BaseModel):
    verified_by_staff: bool | None = None
    notes: str | None = None


class FollowupCreate(BaseModel):
    due_date: str
    entity_type: str = "customer"
    name: str
    phone: str | None = None
    project_id: str | None = None
    action_note: str


class FollowupUpdate(BaseModel):
    status: str | None = None
    due_date: str | None = None
    action_note: str | None = None


class EntityCreate(BaseModel):
    canonical_name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    phone: str | None = None
    notes: str | None = None


class EntityUpdate(BaseModel):
    canonical_name: str | None = None
    entity_type: str | None = None
    aliases: list[str] | None = None
    phone: str | None = None
    notes: str | None = None


class MemoryRuleCreate(BaseModel):
    rule_text: str
    category: str = "style"


# ---------------------------------------------------------------------------
# Auth & Lookup Endpoints
# ---------------------------------------------------------------------------
@router.get("/me")
async def get_me(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "auth_type": user.auth_type,
        "identifier": user.identifier,
        "display_name": user.display_name,
    }


@router.get("/stages")
async def list_stages(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return (
        sb.table("workshop_stages")
        .select("*")
        .order("sort_order")
        .execute()
        .data
        or []
    )


@router.post("/stages", status_code=status.HTTP_201_CREATED)
async def create_stage(
    body: StageCreate, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    slug = body.code or re.sub(r"[^a-z0-9]+", "_", body.label.lower()).strip("_")
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid stage code/label.")

    existing = sb.table("workshop_stages").select("sort_order").order("sort_order", desc=True).limit(1).execute().data
    next_order = body.sort_order if body.sort_order is not None else ((existing[0]["sort_order"] + 1) if existing else 1)

    res = sb.table("workshop_stages").insert(
        {
            "code": slug,
            "label": body.label.strip(),
            "sort_order": next_order,
            "is_terminal": False,
        }
    ).execute()
    return res.data[0]


@router.patch("/stages/{code}")
async def update_stage(
    code: str, body: StageUpdate, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")
    res = sb.table("workshop_stages").update(updates).eq("code", code).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Stage not found.")
    return res.data[0]


@router.post("/stages/reorder")
async def reorder_stages(
    body: StageReorder, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    for idx, code in enumerate(body.codes, start=1):
        sb.table("workshop_stages").update({"sort_order": idx}).eq("code", code).execute()
    return {"ok": True}


@router.get("/material-categories")
async def list_material_categories(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return (
        sb.table("material_categories")
        .select("*")
        .order("sort_order")
        .execute()
        .data
        or []
    )


# ---------------------------------------------------------------------------
# Projects, Items & Sub-Parts
# ---------------------------------------------------------------------------
@router.get("/projects")
async def list_projects(
    status_filter: str | None = Query(default=None, alias="status"),
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    q = sb.table("v_project_pnl").select("*").order("promised_delivery_date")
    if status_filter:
        q = q.eq("status", status_filter)
    projects = q.execute().data or []

    items = sb.table("project_items").select("*").order("created_at").execute().data or []
    parts = sb.table("item_parts").select("*").order("sort_order").execute().data or []

    parts_by_item: dict[str, list[dict[str, Any]]] = {}
    for pt in parts:
        parts_by_item.setdefault(pt["item_id"], []).append(pt)

    items_by_prj: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        it["parts"] = parts_by_item.get(it["id"], [])
        items_by_prj.setdefault(it["project_id"], []).append(it)

    for prj in projects:
        prj["items"] = items_by_prj.get(prj["project_id"], [])
    return projects


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("projects").insert(body.model_dump(exclude_none=True)).execute()
    return res.data[0]


@router.get("/projects/{project_id}")
async def get_project_detail(
    project_id: str, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    pnl = (
        sb.table("v_project_pnl")
        .select("*")
        .eq("project_id", project_id)
        .limit(1)
        .execute()
        .data
    )
    if not pnl:
        raise HTTPException(status_code=404, detail="Project not found.")
    project = pnl[0]
    items = sb.table("project_items").select("*").eq("project_id", project_id).order("created_at").execute().data or []
    materials = sb.table("item_materials").select("*").eq("project_id", project_id).order("created_at").execute().data or []
    parts = sb.table("item_parts").select("*").eq("project_id", project_id).order("sort_order").execute().data or []
    cash = sb.table("cash_ledger").select("*").eq("project_id", project_id).order("txn_date", desc=True).execute().data or []

    mats_by_item: dict[str, list[dict[str, Any]]] = {}
    for m in materials:
        mats_by_item.setdefault(m["item_id"], []).append(m)

    parts_by_item: dict[str, list[dict[str, Any]]] = {}
    for pt in parts:
        parts_by_item.setdefault(pt["item_id"], []).append(pt)

    for it in items:
        it["materials"] = mats_by_item.get(it["id"], [])
        it["parts"] = parts_by_item.get(it["id"], [])

    project["items"] = items
    project["cash_ledger"] = cash
    return project


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    updates = body.model_dump(exclude_none=True)
    res = sb.table("projects").update(updates).eq("id", project_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Project not found.")
    return res.data[0]


@router.post("/projects/{project_id}/items", status_code=status.HTTP_201_CREATED)
async def create_item(
    project_id: str,
    body: ItemCreate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    payload = {**body.model_dump(exclude_none=True), "project_id": project_id}
    res = sb.table("project_items").insert(payload).execute()
    return res.data[0]


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str,
    body: ItemUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    updates = body.model_dump(exclude_none=True)
    res = sb.table("project_items").update(updates).eq("id", item_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Item not found.")

    needed_count = 0
    if body.stage:
        needed = (
            sb.table("item_materials")
            .select("id")
            .eq("item_id", item_id)
            .eq("status", "needed")
            .execute()
            .data
            or []
        )
        needed_count = len(needed)

    return {**res.data[0], "needed_materials_warning_count": needed_count}


@router.post("/items/{item_id}/photos")
async def upload_item_photo(
    item_id: str,
    file: UploadFile = File(...),
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    item_res = (
        sb.table("project_items")
        .select("id,project_id,photo_paths")
        .eq("id", item_id)
        .limit(1)
        .execute()
        .data
    )
    if not item_res:
        raise HTTPException(status_code=404, detail="Item not found.")

    item = item_res[0]
    raw_bytes = await file.read()
    ts = int(datetime.now(timezone.utc).timestamp())
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", file.filename or "photo")
    obj_path = f"projects/{item['project_id']}/{item_id}/photos/{ts}_{safe_name}.webp"
    storage.upload_image_as_webp(obj_path, raw_bytes)

    updated_paths = (item.get("photo_paths") or []) + [obj_path]
    sb.table("project_items").update({"photo_paths": updated_paths}).eq("id", item_id).execute()
    return {"path": obj_path, "photo_paths": updated_paths}


@router.get("/items/{item_id}/parts")
async def list_parts(
    item_id: str, _: CurrentUser = Depends(get_current_user)
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return (
        sb.table("item_parts")
        .select("*")
        .eq("item_id", item_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )


@router.post("/items/{item_id}/parts", status_code=status.HTTP_201_CREATED)
async def create_part(
    item_id: str,
    body: PartCreate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    item_res = sb.table("project_items").select("project_id").eq("id", item_id).limit(1).execute().data
    if not item_res:
        raise HTTPException(status_code=404, detail="Item not found.")
    payload = {
        **body.model_dump(exclude_none=True),
        "item_id": item_id,
        "project_id": item_res[0]["project_id"],
    }
    res = sb.table("item_parts").insert(payload).execute()
    return res.data[0]


@router.patch("/parts/{part_id}")
async def update_part(
    part_id: str,
    body: PartUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("item_parts").update(body.model_dump(exclude_none=True)).eq("id", part_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Part not found.")
    return res.data[0]


@router.delete("/parts/{part_id}")
async def delete_part(
    part_id: str, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    sb.table("item_parts").delete().eq("id", part_id).execute()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Materials & Master Purchase List
# ---------------------------------------------------------------------------
@router.get("/purchase-list")
async def get_master_purchase_list(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return sb.table("v_master_purchase_list").select("*").execute().data or []


@router.post("/items/{item_id}/materials", status_code=status.HTTP_201_CREATED)
async def create_material(
    item_id: str,
    body: MaterialCreate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    item_res = sb.table("project_items").select("project_id").eq("id", item_id).limit(1).execute().data
    if not item_res:
        raise HTTPException(status_code=404, detail="Item not found.")
    payload = {
        **body.model_dump(exclude_none=True),
        "item_id": item_id,
        "project_id": item_res[0]["project_id"],
    }
    res = sb.table("item_materials").insert(payload).execute()
    return res.data[0]


@router.patch("/materials/{material_id}")
async def update_material(
    material_id: str,
    body: MaterialUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("item_materials").update(body.model_dump(exclude_none=True)).eq("id", material_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Material not found.")
    return res.data[0]


# ---------------------------------------------------------------------------
# Cash & Karigar Ledger + CSV Export
# ---------------------------------------------------------------------------
@router.get("/cash-ledger")
async def list_cash_ledger(
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    rows = sb.table("cash_ledger").select("*").order("txn_date", desc=True).order("created_at", desc=True).limit(500).execute().data or []
    balance = sum(
        float(r["amount_inr"]) if r["direction"] == "in" else -float(r["amount_inr"])
        for r in rows
    )
    return {"balance_inr": round(balance, 2), "rows": rows}


@router.post("/cash-ledger", status_code=status.HTTP_201_CREATED)
async def create_cash_entry(
    body: CashLedgerCreate,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    payload = {**body.model_dump(exclude_none=True), "logged_by": user.display_name}
    res = sb.table("cash_ledger").insert(payload).execute()
    return res.data[0]


@router.patch("/cash-ledger/{txn_id}")
async def update_cash_entry(
    txn_id: str,
    body: CashLedgerUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("cash_ledger").update(body.model_dump(exclude_none=True)).eq("id", txn_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Cash entry not found.")
    return res.data[0]


@router.get("/cash-ledger/export.csv")
async def export_cash_ledger_csv(
    _: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    sb = get_supabase_admin()
    rows = sb.table("cash_ledger").select("*").order("txn_date", desc=True).execute().data or []
    buf = StringIO()
    writer = csv.writer(buf)
    headers = ["txn_date", "txn_type", "direction", "party_or_karigar", "amount_inr", "payment_mode", "verified_by_staff", "notes", "logged_by"]
    writer.writerow(headers)
    for r in rows:
        writer.writerow([r.get(h, "") for h in headers])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cash_ledger.csv"},
    )


# ---------------------------------------------------------------------------
# Follow-Ups, Entity Dictionary, Historical Archive & Signed Media URLs
# ---------------------------------------------------------------------------
@router.get("/followups")
async def list_followups(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return sb.table("followups").select("*").order("status", desc=True).order("due_date").execute().data or []


@router.post("/followups", status_code=status.HTTP_201_CREATED)
async def create_followup(
    body: FollowupCreate, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    return sb.table("followups").insert(body.model_dump(exclude_none=True)).execute().data[0]


@router.patch("/followups/{followup_id}")
async def update_followup(
    followup_id: str,
    body: FollowupUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("followups").update(body.model_dump(exclude_none=True)).eq("id", followup_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    return res.data[0]


@router.get("/entities")
async def list_entities(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return sb.table("entity_dictionary").select("*").order("canonical_name").execute().data or []


@router.post("/entities", status_code=status.HTTP_201_CREATED)
async def create_entity(
    body: EntityCreate, _: CurrentUser = Depends(get_current_user)
) -> dict[str, Any]:
    sb = get_supabase_admin()
    return sb.table("entity_dictionary").insert(body.model_dump(exclude_none=True)).execute().data[0]


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    body: EntityUpdate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("entity_dictionary").update(body.model_dump(exclude_none=True)).eq("id", entity_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return res.data[0]


@router.get("/memory")
async def list_memory_rules(
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    return (
        sb.table("workshop_memory")
        .select("*")
        .eq("is_active", True)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )


@router.post("/memory", status_code=status.HTTP_201_CREATED)
async def create_memory_rule(
    body: MemoryRuleCreate,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    res = sb.table("workshop_memory").insert(
        {
            "rule_text": body.rule_text.strip(),
            "category": body.category.strip() or "style",
            "source": "manual",
        }
    ).execute()
    return res.data[0]


@router.delete("/memory/{rule_id}")
async def delete_memory_rule(
    rule_id: str,
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, bool]:
    sb = get_supabase_admin()
    sb.table("workshop_memory").delete().eq("id", rule_id).execute()
    return {"deleted": True}


@router.get("/archive")
async def search_archive(
    q: str = Query(default=""),
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = get_supabase_admin()
    query = sb.table("historical_archive").select("*").order("doc_date", desc=True).limit(100)
    if q.strip():
        query = query.ilike("party_name", f"%{q.strip()}%")
    return query.execute().data or []


@router.get("/media/signed-url")
async def get_media_signed_url(
    path: str = Query(...),
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, str]:
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid object path.")
    url = storage.generate_signed_url(path, expiration_minutes=30)
    return {"url": url}
