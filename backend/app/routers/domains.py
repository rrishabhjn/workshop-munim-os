"""Modular REST Endpoints for Domain 1 (/production), Domain 2 (/munim), and AgentCore (/api/agent).

All routes require an authenticated allowlisted operator via `get_current_user`
(Google Sign-In Bearer JWT or verified Telegram Mini App initData HMAC).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user
from app.db import get_supabase_admin
from app.services import agent_core, step_executors, zoho

router = APIRouter(prefix="/api", tags=["domains"])
logger = logging.getLogger(__name__)


def _resolve_actor_access(sb: Any, user: CurrentUser) -> tuple[str | None, str, bool]:
    """Map authenticated CurrentUser to (people.id, full_name, can_login_munim)."""
    if user.auth_type == "telegram" and user.identifier.isdigit():
        rows = (
            sb.table("people")
            .select("id,full_name,can_login_munim")
            .eq("telegram_user_id", int(user.identifier))
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            return str(rows[0]["id"]), str(rows[0]["full_name"]), rows[0].get("can_login_munim") is not False
    else:
        rows = (
            sb.table("people")
            .select("id,full_name,can_login_munim")
            .ilike("email", user.identifier)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows:
            return str(rows[0]["id"]), str(rows[0]["full_name"]), rows[0].get("can_login_munim") is not False
    return None, user.display_name, True


def _resolve_actor_person(sb: Any, user: CurrentUser) -> tuple[str | None, str]:
    """Map authenticated CurrentUser (email or telegram_user_id) to canonical people.id."""
    actor_id, actor_name, _ = _resolve_actor_access(sb, user)
    return actor_id, actor_name


def _require_munim_access(sb: Any, user: CurrentUser) -> tuple[str | None, str]:
    """Reject operators whose people.can_login_munim flag is explicitly False."""
    actor_id, actor_name, can_munim = _resolve_actor_access(sb, user)
    if not can_munim:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Munim finance portal access is disabled for your account.",
        )
    return actor_id, actor_name


# ---------------------------------------------------------------------------
# Request Payloads
# ---------------------------------------------------------------------------
class OrderCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    area_landmark: str = Field(min_length=1, max_length=120)
    rough_scope: str = Field(min_length=1, max_length=500)
    phone: str | None = None
    quoted_total: float = 0.0
    promised_delivery_date: str | None = None
    existing_customer_id: str | None = None


class OrderPatchRequest(BaseModel):
    customer_name: str | None = None
    site_area_landmark: str | None = None
    rough_scope: str | None = None
    phone: str | None = None
    quoted_total: float | None = None
    promised_delivery_date: str | None = None
    boq_scope_changed: bool | None = None
    boq_scope_note: str | None = None
    payment_stage: str | None = None


class PipelineMoveRequest(BaseModel):
    to_stage: str
    estimator_ids: list[str] | None = None
    sizing_mistri_id: str | None = None
    boq_scope_changed: bool | None = None
    boq_scope_note: str | None = None
    note: str | None = None


class MaterialMoveRequest(BaseModel):
    to_stage: str
    material_owner_id: str | None = None
    note: str | None = None


class FlatItemUpsertRequest(BaseModel):
    item_name: str | None = None
    to_stage: str = "in_shop"
    production_mistri_id: str | None = None
    assembly_mistri_id: str | None = None
    qc_passed_by_id: str | None = None
    dimensions: str | None = None
    specs_notes: str | None = None


class UndoEventsRequest(BaseModel):
    event_ids: list[str]


class ItemMaterialCreateRequest(BaseModel):
    material_name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(default=1.0, gt=0)
    unit: str = Field(default="pcs", max_length=40)
    category: str | None = None
    status: str = "needed"
    notes: str | None = None


class ItemMaterialPatchRequest(BaseModel):
    material_name: str | None = None
    quantity: float | None = Field(default=None, gt=0)
    unit: str | None = None
    category: str | None = None
    status: str | None = None
    notes: str | None = None


class BulkMaterialStatusRequest(BaseModel):
    material_ids: list[str] = Field(min_length=1)
    status: str


class PersonCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    person_type: str = "salaried_karigar"
    role_tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    payout_aliases: list[str] = Field(default_factory=list)
    email: str | None = None
    telegram_user_id: int | None = None
    phone: str | None = None
    is_default_purchase: bool = False
    is_default_qc: bool = False


class PersonPatchRequest(BaseModel):
    full_name: str | None = None
    person_type: str | None = None
    role_tags: list[str] | None = None
    aliases: list[str] | None = None
    payout_aliases: list[str] | None = None
    email: str | None = None
    phone: str | None = None
    is_active: bool | None = None


class VendorCreateRequest(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=140)
    vendor_kind: str = "supplier"
    specialty: str | None = None
    phone: str | None = None
    gstin: str | None = None
    aliases: list[str] = Field(default_factory=list)
    notes: str | None = None


class VendorPatchRequest(BaseModel):
    canonical_name: str | None = None
    vendor_kind: str | None = None
    specialty: str | None = None
    phone: str | None = None
    gstin: str | None = None
    aliases: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class CustomerCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=140)
    area_landmark: str | None = None
    phone: str | None = None
    aliases: list[str] = Field(default_factory=list)
    notes: str | None = None


class CustomerPatchRequest(BaseModel):
    full_name: str | None = None
    area_landmark: str | None = None
    phone: str | None = None
    aliases: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class GoalChoiceRequest(BaseModel):
    slot_patch: dict[str, Any] = Field(default_factory=dict)


class GoalEditRequest(BaseModel):
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    correction_note: str | None = None


# ---------------------------------------------------------------------------
# Domain 1: Production Lifecycle Endpoints (/api/production/*)
# ---------------------------------------------------------------------------
@router.get("/production/overview")
async def get_production_overview(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, actor_name = _resolve_actor_person(sb, user)

    pipeline_stages = sb.table("project_pipeline_stages").select("*").order("sort_order").execute().data or []
    material_stages = sb.table("material_track_stages").select("*").order("sort_order").execute().data or []
    payment_stages = sb.table("payment_track_stages").select("*").order("sort_order").execute().data or []
    item_stages = sb.table("workshop_stages").select("*").order("sort_order").execute().data or []
    material_categories = sb.table("material_categories").select("*").order("sort_order").execute().data or []

    people = (
        sb.table("people")
        .select("*")
        .eq("is_active", True)
        .order("full_name")
        .execute()
        .data
        or []
    )
    customers = (
        sb.table("customers")
        .select("*")
        .eq("is_active", True)
        .order("full_name")
        .limit(200)
        .execute()
        .data
        or []
    )
    orders = (
        sb.table("projects")
        .select("*")
        .eq("project_type", "customer")
        .order("updated_at", desc=True)
        .limit(120)
        .execute()
        .data
        or []
    )
    order_ids = [o["id"] for o in orders]
    items: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    if order_ids:
        items = (
            sb.table("project_items")
            .select("*")
            .in_("project_id", order_ids)
            .order("created_at")
            .execute()
            .data
            or []
        )
        materials = (
            sb.table("item_materials")
            .select("*")
            .in_("project_id", order_ids)
            .order("created_at")
            .execute()
            .data
            or []
        )

    materials_by_item: dict[str, list[dict[str, Any]]] = {}
    materials_by_order: dict[str, list[dict[str, Any]]] = {}
    for m in materials:
        materials_by_item.setdefault(m["item_id"], []).append(m)
        materials_by_order.setdefault(m["project_id"], []).append(m)

    items_by_order: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        enriched_item = {**it, "materials": materials_by_item.get(it["id"], [])}
        items_by_order.setdefault(it["project_id"], []).append(enriched_item)

    enriched_orders = [
        {
            **o,
            "items": items_by_order.get(o["id"], []),
            "item_materials": materials_by_order.get(o["id"], []),
        }
        for o in orders
    ]
    whos_doing_what = step_executors.enrich_whos_doing_what_rows(
        sb, sb.table("v_whos_doing_what").select("*").execute().data or []
    )
    needs_info = sb.table("v_needs_info_queue").select("*").execute().data or []
    actor_id, actor_name, can_munim = _resolve_actor_access(sb, user)

    return {
        "me": {
            "person_id": actor_id,
            "display_name": actor_name,
            "identifier": user.identifier,
            "auth_type": user.auth_type,
            "can_login_munim": can_munim,
        },
        "lookups": {
            "pipeline_stages": pipeline_stages,
            "material_stages": material_stages,
            "payment_stages": payment_stages,
            "item_stages": item_stages,
            "material_categories": material_categories,
        },
        "people": people,
        "customers": customers,
        "orders": enriched_orders,
        "whos_doing_what": whos_doing_what,
        "needs_info": needs_info,
    }


@router.post("/production/orders")
async def create_production_order(
    body: OrderCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    return step_executors.create_customer_and_lead(
        sb,
        full_name=body.full_name,
        area_landmark=body.area_landmark,
        rough_scope=body.rough_scope,
        phone=body.phone,
        quoted_total=body.quoted_total,
        promised_delivery_date=body.promised_delivery_date,
        existing_customer_id=body.existing_customer_id,
        actor_person_id=actor_id,
    )


@router.patch("/production/orders/{project_id}")
async def patch_production_order(
    project_id: str,
    body: OrderPatchRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    res = step_executors.update_order_metadata(
        sb,
        project_id=project_id,
        quoted_total=body.quoted_total,
        promised_delivery_date=body.promised_delivery_date,
        rough_scope=body.rough_scope,
        site_area_landmark=body.site_area_landmark,
        phone=body.phone,
        boq_scope_changed=body.boq_scope_changed,
        boq_scope_note=body.boq_scope_note,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    extra_patch: dict[str, Any] = {}
    if body.customer_name is not None and body.customer_name.strip():
        extra_patch["customer_name"] = body.customer_name.strip()
    if body.payment_stage is not None:
        extra_patch["payment_stage"] = body.payment_stage
    if extra_patch:
        upd = sb.table("projects").update(extra_patch).eq("id", project_id).execute().data
        if upd:
            res["order"] = upd[0]
    return res


@router.post("/production/orders/{project_id}/pipeline")
async def move_order_pipeline_stage(
    project_id: str,
    body: PipelineMoveRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await step_executors.transition_project_pipeline(
        sb,
        project_id=project_id,
        to_stage=body.to_stage,
        actor_person_id=actor_id,
        estimator_ids=body.estimator_ids,
        sizing_mistri_id=body.sizing_mistri_id,
        boq_scope_changed=body.boq_scope_changed,
        boq_scope_note=body.boq_scope_note,
        source_channel="web",
        note=body.note,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Invalid transition"))
    return res


@router.post("/production/orders/{project_id}/material")
async def move_order_material_stage(
    project_id: str,
    body: MaterialMoveRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await step_executors.transition_material_track(
        sb,
        project_id=project_id,
        to_stage=body.to_stage,
        actor_person_id=actor_id,
        material_owner_id=body.material_owner_id,
        source_channel="web",
        note=body.note,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Invalid transition"))
    return res


@router.post("/production/orders/{project_id}/items")
async def create_order_flat_item(
    project_id: str,
    body: FlatItemUpsertRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await step_executors.upsert_or_transition_item(
        sb,
        project_id=project_id,
        item_name=body.item_name,
        to_stage=body.to_stage,
        production_mistri_id=body.production_mistri_id,
        assembly_mistri_id=body.assembly_mistri_id,
        dimensions=body.dimensions,
        specs_notes=body.specs_notes,
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Failed adding item"))
    return res


@router.patch("/production/items/{item_id}")
async def update_order_flat_item(
    item_id: str,
    body: FlatItemUpsertRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    cur = sb.table("project_items").select("project_id,stage").eq("id", item_id).limit(1).execute().data
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")
    res = await step_executors.upsert_or_transition_item(
        sb,
        project_id=cur[0]["project_id"],
        item_id=item_id,
        to_stage=body.to_stage or cur[0]["stage"],
        production_mistri_id=body.production_mistri_id,
        assembly_mistri_id=body.assembly_mistri_id,
        qc_passed_by_id=body.qc_passed_by_id,
        dimensions=body.dimensions,
        specs_notes=body.specs_notes,
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Failed updating item"))
    return res


@router.post("/production/items/{item_id}/materials")
async def add_item_raw_material(
    item_id: str,
    body: ItemMaterialCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    cur = sb.table("project_items").select("project_id").eq("id", item_id).limit(1).execute().data
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")
    res = await step_executors.add_item_materials_batch(
        sb,
        project_id=cur[0]["project_id"],
        item_id=item_id,
        materials=[body.model_dump()],
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Failed adding material"))
    return res


@router.patch("/production/materials/{material_id}")
async def patch_item_raw_material(
    material_id: str,
    body: ItemMaterialPatchRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = step_executors.update_item_materials_rows(
        sb,
        material_ids=[material_id],
        patch=body.model_dump(exclude_none=True),
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Failed updating material"))
    return res


@router.post("/production/orders/{project_id}/materials/bulk-status")
async def bulk_update_order_materials_status(
    project_id: str,
    body: BulkMaterialStatusRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    rows = (
        sb.table("item_materials")
        .select("id")
        .eq("project_id", project_id)
        .in_("id", body.material_ids)
        .execute()
        .data
        or []
    )
    valid_ids = [r["id"] for r in rows]
    if not valid_ids:
        raise HTTPException(status_code=404, detail="No matching material lines on this order")
    res = step_executors.update_item_materials_rows(
        sb,
        material_ids=valid_ids,
        patch={"status": body.status},
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Failed updating material status"))
    return res


@router.delete("/production/materials/{material_id}")
async def delete_item_raw_material(
    material_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = step_executors.delete_item_material_row(
        sb,
        material_id=material_id,
        actor_person_id=actor_id,
        source_channel="web",
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", {}).get("message", "Material line not found"))
    return res


@router.post("/production/undo")
async def undo_production_events(
    body: UndoEventsRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    reverted = step_executors.undo_stage_events(sb, body.event_ids, actor_person_id=actor_id)
    return {"ok": True, "reverted": reverted}


@router.get("/production/orders/{project_id}/timeline")
async def get_order_timeline(
    project_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _ = user
    sb = get_supabase_admin()
    events = (
        sb.table("stage_events")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(60)
        .execute()
        .data
        or []
    )
    people_map = {
        p["id"]: p["full_name"]
        for p in (sb.table("people").select("id,full_name").execute().data or [])
    }
    items_map = {
        i["id"]: i["item_name"]
        for i in (sb.table("project_items").select("id,item_name").eq("project_id", project_id).execute().data or [])
    }
    return [
        {
            **e,
            "actor_name": people_map.get(e.get("actor_person_id"), "System"),
            "assigned_names": [people_map.get(pid, "") for pid in (e.get("assigned_people_ids") or []) if pid in people_map],
            "item_name": items_map.get(e.get("item_id")),
        }
        for e in events
    ]


# ---------------------------------------------------------------------------
# Domain 2: Munim Finance & Master Spine Endpoints (/api/munim/*)
#   (Worker Attendance & Monthly FnF Payroll intentionally parked for Wave 2)
# ---------------------------------------------------------------------------
@router.get("/munim/overview")
async def get_munim_overview(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, actor_name = _require_munim_access(sb, user)
    accounts = sb.table("payment_accounts").select("*").eq("is_active", True).order("sort_order").execute().data or []
    pnl = sb.table("v_project_pnl").select("*").eq("project_type", "customer").order("promised_delivery_date").limit(80).execute().data or []
    customers = sb.table("customers").select("*").eq("is_active", True).order("full_name").limit(200).execute().data or []
    vendors = sb.table("vendors").select("*").eq("is_active", True).order("canonical_name").limit(200).execute().data or []
    people = sb.table("people").select("*").eq("is_active", True).order("full_name").limit(200).execute().data or []
    needs_info = sb.table("v_needs_info_queue").select("*").execute().data or []

    return {
        "me": {"person_id": actor_id, "display_name": actor_name, "can_login_munim": True},
        "payment_accounts": accounts,
        "project_pnl": pnl,
        "customers": customers,
        "vendors": vendors,
        "people": people,
        "needs_info": needs_info,
        "zoho_enabled": zoho.get_settings().zoho_enabled,
    }


@router.post("/munim/sync-zoho")
async def trigger_zoho_pulse_sync(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    _require_munim_access(sb, user)
    pulse = await zoho.sync_owner_pulse_snapshot(sb)
    project_caches = await zoho.sync_project_financial_caches(sb)
    return {**pulse, "project_caches": project_caches}


@router.post("/munim/people")
async def add_person_to_roster(
    body: PersonCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    payload = body.model_dump(exclude_none=True)
    if body.person_type == "operator":
        payload["can_login_prod"] = True
        payload["can_login_munim"] = True
    res = sb.table("people").insert(payload).execute().data
    return {"ok": True, "person": res[0]}


@router.patch("/munim/people/{person_id}")
async def patch_person_in_roster(
    person_id: str,
    body: PersonPatchRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    res = step_executors.update_person_record(
        sb,
        person_id=person_id,
        full_name=body.full_name,
        person_type=body.person_type,
        role_tags=body.role_tags,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases,
        payout_aliases=body.payout_aliases,
        is_active=body.is_active,
        replace_lists=True,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", {}).get("message", "Person not found"))
    return res


@router.post("/munim/vendors")
async def add_vendor_to_directory(
    body: VendorCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    _require_munim_access(sb, user)
    payload = body.model_dump(exclude_none=True)
    if payload.get("gstin"):
        payload["gstin"] = str(payload["gstin"]).strip().upper()
    res = sb.table("vendors").insert(payload).execute().data
    return {"ok": True, "vendor": res[0]}


@router.patch("/munim/vendors/{vendor_id}")
async def patch_vendor_in_directory(
    vendor_id: str,
    body: VendorPatchRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    _require_munim_access(sb, user)
    res = await step_executors.update_vendor_record(
        sb,
        vendor_id=vendor_id,
        canonical_name=body.canonical_name,
        vendor_kind=body.vendor_kind,
        phone=body.phone,
        gstin=body.gstin,
        specialty=body.specialty,
        aliases=body.aliases,
        notes=body.notes,
        is_active=body.is_active,
        replace_lists_and_notes=True,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", {}).get("message", "Vendor not found"))
    return res


@router.post("/munim/customers")
async def add_customer_to_directory(
    body: CustomerCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    _require_munim_access(sb, user)
    res = step_executors.create_standalone_customer(
        sb,
        full_name=body.full_name,
        area_landmark=body.area_landmark,
        phone=body.phone,
        aliases=body.aliases,
        notes=body.notes,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", {}).get("message", "Invalid customer"))
    return res


@router.patch("/munim/customers/{customer_id}")
async def patch_customer_in_directory(
    customer_id: str,
    body: CustomerPatchRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    _require_munim_access(sb, user)
    res = await step_executors.update_customer_record(
        sb,
        customer_id=customer_id,
        full_name=body.full_name,
        area_landmark=body.area_landmark,
        phone=body.phone,
        aliases=body.aliases,
        notes=body.notes,
        is_active=body.is_active,
        replace_lists_and_notes=True,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", {}).get("message", "Customer not found"))
    return res


# ---------------------------------------------------------------------------
# Omnichannel AgentCore Endpoints (/api/agent/*)
# ---------------------------------------------------------------------------
@router.get("/agent/needs-info")
async def list_needs_info_queue(
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _ = user
    sb = get_supabase_admin()
    return sb.table("v_needs_info_queue").select("*").execute().data or []


def _validate_goal_id_or_400(sb: Any, goal_id: str) -> str:
    if hasattr(sb, "store"):
        return goal_id
    try:
        return str(uuid.UUID(str(goal_id).strip()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid goal ID.") from exc


@router.post("/agent/turn")
async def execute_web_agent_turn(
    text: str = Form(default=""),
    domain_scope: str = Form(default="production"),
    ui_context_json: str = Form(default="{}"),
    audio: UploadFile | None = File(default=None),
    attachment: UploadFile | None = File(default=None),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    actor_id, actor_name, can_munim = _resolve_actor_access(sb, user)
    scope = "munim" if domain_scope == "munim" else "production"
    if scope == "munim" and not can_munim:
        return {
            "kind": "error",
            "is_error": True,
            "reply_text": "🔒 Munim finance assistant access is disabled for your account. Please switch to Production.",
            "goals": [],
        }

    try:
        ui_ctx = json.loads(ui_context_json) if ui_context_json else {}
    except json.JSONDecodeError:
        ui_ctx = {}

    audio_bytes: bytes | None = None
    audio_mime = "audio/webm"
    if audio is not None:
        audio_bytes = await audio.read()
        audio_mime = audio.content_type or "audio/webm"

    media_files: list[tuple[bytes, str]] = []
    if attachment is not None:
        att_bytes = await attachment.read()
        att_mime = attachment.content_type or "image/jpeg"
        media_files.append((att_bytes, att_mime))

    try:
        return await agent_core.run_agent_turn(
            sb,
            owner_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web_command_bar",
            domain_scope=scope,
            text=text or None,
            audio_bytes=audio_bytes,
            audio_mime_type=audio_mime,
            media_files=media_files or None,
            ui_context=ui_ctx,
        )
    except Exception as exc:
        logger.exception("Unhandled error in /api/agent/turn: %s", exc)
        return {
            "kind": "error",
            "is_error": True,
            "reply_text": "⚠️ Could not process that command cleanly — your text is preserved below so you can adjust or retry.",
            "goals": [],
        }


@router.post("/agent/goals/{goal_id}/confirm")
async def confirm_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, actor_name = _resolve_actor_person(sb, user)
    try:
        return await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=gid,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web",
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/confirm: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not execute goal safely.") from exc


@router.post("/agent/goals/{goal_id}/choice")
async def choose_agent_goal_option(
    goal_id: str,
    body: GoalChoiceRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, actor_name = _resolve_actor_person(sb, user)
    try:
        return await agent_core.apply_slot_patch_and_resume(
            sb,
            goal_id=gid,
            slot_patch=body.slot_patch,
            actor_person_id=actor_id,
            actor_label=actor_name,
            source_channel="web",
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/choice: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not apply option safely.") from exc


@router.post("/agent/goals/{goal_id}/undo")
async def undo_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    actor_id, _ = _resolve_actor_person(sb, user)
    try:
        return agent_core.undo_completed_goal(sb, goal_id=gid, actor_person_id=actor_id)
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/undo: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not undo action safely.") from exc


@router.post("/agent/goals/{goal_id}/park")
async def park_agent_goal_for_clerk(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    return agent_core.park_goal_for_clerk(sb, gid)


@router.post("/agent/goals/{goal_id}/discard")
async def discard_agent_goal(
    goal_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    return agent_core.discard_goal(sb, gid)


@router.post("/agent/goals/{goal_id}/edit")
async def edit_agent_goal(
    goal_id: str,
    body: GoalEditRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = get_supabase_admin()
    gid = _validate_goal_id_or_400(sb, goal_id)
    try:
        return await asyncio.to_thread(
            agent_core.edit_open_goal_slots,
            sb,
            goal_id=gid,
            slot_patch=body.slot_patch,
            correction_note=body.correction_note,
        )
    except Exception as exc:
        logger.exception("Unhandled error in /agent/goals/%s/edit: %s", gid, exc)
        raise HTTPException(status_code=400, detail="Could not update goal edits cleanly.") from exc
