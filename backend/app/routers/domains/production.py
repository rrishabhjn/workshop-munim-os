"""Domain 1: Production Lifecycle Endpoints (`/api/production/*`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import CurrentUser, get_current_user
from .common import (
    BulkMaterialStatusRequest,
    FlatItemUpsertRequest,
    ItemMaterialCreateRequest,
    ItemMaterialPatchRequest,
    MaterialMoveRequest,
    OrderCreateRequest,
    OrderPatchRequest,
    PipelineMoveRequest,
    UndoEventsRequest,
    _pkg,
    _resolve_actor_access,
    _resolve_actor_person,
)

router = APIRouter()


@router.get("/production/overview")
async def get_production_overview(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
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
    whos_doing_what = pkg.step_executors.enrich_whos_doing_what_rows(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    return pkg.step_executors.create_customer_and_lead(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    res = pkg.step_executors.update_order_metadata(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.transition_project_pipeline(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.transition_material_track(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.upsert_or_transition_item(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    cur = sb.table("project_items").select("project_id,stage").eq("id", item_id).limit(1).execute().data
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")
    res = await pkg.step_executors.upsert_or_transition_item(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    cur = sb.table("project_items").select("project_id").eq("id", item_id).limit(1).execute().data
    if not cur:
        raise HTTPException(status_code=404, detail="Item not found")
    res = await pkg.step_executors.add_item_materials_batch(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = pkg.step_executors.update_item_materials_rows(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
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
    res = pkg.step_executors.update_item_materials_rows(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = pkg.step_executors.delete_item_material_row(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    reverted = pkg.step_executors.undo_stage_events(sb, body.event_ids, actor_person_id=actor_id)
    return {"ok": True, "reverted": reverted}


@router.get("/production/orders/{project_id}/timeline")
async def get_order_timeline(
    project_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _ = user
    sb = _pkg().get_supabase_admin()
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
