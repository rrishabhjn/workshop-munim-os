"""Domain 3: Workshop Inventory & Procurement Endpoints (`/api/inventory/*`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser, get_current_user
from .common import _pkg, _resolve_actor_person

router = APIRouter()


class InventorySkuUpsertRequest(BaseModel):
    name: str = ""
    category: str | None = None
    unit: str = "pcs"
    min_stock_qty: float = 0.0
    avg_unit_cost_inr: float | None = None
    aliases: list[str] | None = None
    notes: str | None = None
    is_active: bool | None = None


class InventoryLoadLinePayload(BaseModel):
    destination: str = "stock"
    inventory_item_id: str | None = None
    material_name: str = ""
    category: str | None = None
    quantity: float = Field(gt=0)
    unit: str = "pcs"
    unit_rate_inr: float | None = Field(default=None, ge=0)
    min_stock_qty: float = 0.0
    project_id: str | None = None
    item_id: str | None = None
    matched_item_material_id: str | None = None


class InventoryLoadRequest(BaseModel):
    settlement_mode: str = "manual"
    lines: list[InventoryLoadLinePayload] = Field(min_length=1)
    payment_account_id: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    invoice_no: str | None = None
    txn_date: str | None = None
    notes: str | None = None


class InventoryAttributeRequest(BaseModel):
    inventory_item_id: str
    project_id: str
    quantity: float = Field(gt=0)
    item_id: str | None = None
    item_material_id: str | None = None
    notes: str | None = None


class InventoryCountAdjustRequest(BaseModel):
    inventory_item_id: str
    actual_qty: float = Field(ge=0)
    notes: str | None = None


class InventoryEnterRateLinePayload(BaseModel):
    movement_id: str
    unit_rate_inr: float = Field(gt=0)


class InventoryEnterRateRequest(BaseModel):
    settlement_mode: str = "paid_now"
    unit_rate_inr: float | None = None
    line_rates: list[InventoryEnterRateLinePayload] | None = None
    payment_account_id: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    invoice_no: str | None = None
    txn_date: str | None = None
    notes: str | None = None


def _unwrap_ok(res: dict[str, Any], fallback_msg: str = "Inventory operation failed.") -> dict[str, Any]:
    if not res.get("ok"):
        err = res.get("error")
        detail = err.get("message") if isinstance(err, dict) else (str(err) if err else fallback_msg)
        raise HTTPException(status_code=400, detail=detail or fallback_msg)
    return res


@router.get("/inventory/overview")
async def get_inventory_overview(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Return full Workshop Inventory snapshot (uniformly accessible to all logged-in operators)."""
    _ = user
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    return pkg.step_executors.get_inventory_overview_data(sb)


@router.post("/inventory/items")
async def create_inventory_sku(
    body: InventorySkuUpsertRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a standing Workshop Stock SKU (or reuse existing case-insensitive duplicate)."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _resolve_actor_person(sb, user)
    res = pkg.step_executors.upsert_inventory_item(
        sb,
        name=body.name,
        category=body.category,
        unit=body.unit,
        min_stock_qty=body.min_stock_qty,
        avg_unit_cost_inr=body.avg_unit_cost_inr,
        aliases=body.aliases,
        notes=body.notes,
        is_active=body.is_active,
    )
    return _unwrap_ok(res, "Failed creating SKU")


@router.patch("/inventory/items/{sku_id}")
async def patch_inventory_sku(
    sku_id: str,
    body: InventorySkuUpsertRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Edit an existing standing Workshop Stock SKU."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _resolve_actor_person(sb, user)
    res = pkg.step_executors.upsert_inventory_item(
        sb,
        sku_id=sku_id,
        name=body.name,
        category=body.category,
        unit=body.unit,
        min_stock_qty=body.min_stock_qty,
        avg_unit_cost_inr=body.avg_unit_cost_inr,
        aliases=body.aliases,
        notes=body.notes,
        is_active=body.is_active,
    )
    return _unwrap_ok(res, "Failed updating SKU")


@router.post("/inventory/load")
async def load_inventory_batch(
    body: InventoryLoadRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Load incoming material lines into Workshop Stock and/or direct Customer Orders."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.execute_inventory_load(
        sb,
        settlement_mode=body.settlement_mode,
        lines=[ln.model_dump() for ln in body.lines],
        payment_account_id=body.payment_account_id,
        vendor_id=body.vendor_id,
        vendor_name=body.vendor_name,
        invoice_no=body.invoice_no,
        txn_date=body.txn_date,
        notes=body.notes,
        actor_person_id=actor_id,
        source_channel="web",
    )
    return _unwrap_ok(res, "Failed loading inventory")


@router.post("/inventory/attribute")
async def attribute_stock_to_order(
    body: InventoryAttributeRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Pull reusable stock from godown onto a Customer Order (furniture item optional)."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.execute_customer_stock_attribution(
        sb,
        inventory_item_id=body.inventory_item_id,
        project_id=body.project_id,
        quantity=body.quantity,
        item_id=body.item_id,
        item_material_id=body.item_material_id,
        notes=body.notes,
        actor_person_id=actor_id,
        source_channel="web",
    )
    return _unwrap_ok(res, "Failed attributing stock to order")


@router.post("/inventory/count-adjust")
async def adjust_inventory_count(
    body: InventoryCountAdjustRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Adjust SKU physical stock count and write off variance to General Shop COGS."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.execute_inventory_count_adjust(
        sb,
        inventory_item_id=body.inventory_item_id,
        actual_qty=body.actual_qty,
        notes=body.notes,
        actor_person_id=actor_id,
        source_channel="web",
    )
    return _unwrap_ok(res, "Failed adjusting count")


@router.post("/inventory/movements/{movement_id}/enter-rate")
async def enter_rate_on_pending_movement(
    movement_id: str,
    body: InventoryEnterRateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Price a Rate Pending movement and push to Zoho without double-adding on_hand_qty."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _resolve_actor_person(sb, user)
    line_rates_map = (
        {lr.movement_id: lr.unit_rate_inr for lr in body.line_rates}
        if body.line_rates
        else None
    )
    res = await pkg.step_executors.complete_rate_pending_movement(
        sb,
        movement_id=movement_id,
        settlement_mode=body.settlement_mode,
        unit_rate_inr=body.unit_rate_inr,
        line_rates=line_rates_map,
        payment_account_id=body.payment_account_id,
        vendor_id=body.vendor_id,
        vendor_name=body.vendor_name,
        invoice_no=body.invoice_no,
        notes=body.notes,
    )
    return _unwrap_ok(res, "Failed entering rate")


@router.post("/inventory/movements/{movement_id}/undo")
async def undo_inventory_ledger_movement(
    movement_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Same-day IST undo for an inventory movement/batch (reverses Zoho & restores stock)."""
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    actor_id, _ = _resolve_actor_person(sb, user)
    res = await pkg.step_executors.undo_inventory_movement(
        sb,
        movement_id,
        actor_person_id=actor_id,
    )
    return _unwrap_ok(res, "Failed undoing movement")
