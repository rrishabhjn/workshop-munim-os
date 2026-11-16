"""Modular REST Endpoints for Domain 1 (/production), Domain 2 (/munim), and AgentCore (/api/agent).

All routes require an authenticated allowlisted operator via `get_current_user`
(Google Sign-In Bearer JWT or verified Telegram Mini App initData HMAC).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.db import get_supabase_admin
from app.services import agent_core, step_executors, storage, zoho

from .agent import (
    choose_agent_goal_option,
    confirm_agent_goal,
    discard_agent_goal,
    edit_agent_goal,
    execute_web_agent_turn,
    list_needs_info_queue,
    park_agent_goal_for_clerk,
    router as agent_router,
    undo_agent_goal,
)
from .common import (
    BulkMaterialStatusRequest,
    CustomerCreateRequest,
    CustomerPatchRequest,
    FlatItemUpsertRequest,
    GoalChoiceRequest,
    GoalEditRequest,
    ItemMaterialCreateRequest,
    ItemMaterialPatchRequest,
    MaterialMoveRequest,
    OrderCreateRequest,
    OrderPatchRequest,
    PersonCreateRequest,
    PersonPatchRequest,
    PipelineMoveRequest,
    UndoEventsRequest,
    VendorCreateRequest,
    VendorPatchRequest,
    _require_munim_access,
    _resolve_actor_access,
    _resolve_actor_person,
    _validate_goal_id_or_400,
)
from .munim import (
    add_customer_to_directory,
    add_person_to_roster,
    add_vendor_to_directory,
    get_media_signed_url,
    get_munim_ledger,
    get_munim_overview,
    patch_customer_in_directory,
    patch_person_in_roster,
    patch_vendor_in_directory,
    router as munim_router,
    search_archive,
    trigger_zoho_pulse_sync,
)
from .production import (
    add_item_raw_material,
    bulk_update_order_materials_status,
    create_order_flat_item,
    create_production_order,
    delete_item_raw_material,
    get_order_timeline,
    get_production_overview,
    move_order_material_stage,
    move_order_pipeline_stage,
    patch_item_raw_material,
    patch_production_order,
    router as production_router,
    undo_production_events,
    update_order_flat_item,
)

router = APIRouter(prefix="/api", tags=["domains"])
router.include_router(production_router)
router.include_router(munim_router)
router.include_router(agent_router)

__all__ = [
    "BulkMaterialStatusRequest",
    "CustomerCreateRequest",
    "CustomerPatchRequest",
    "FlatItemUpsertRequest",
    "GoalChoiceRequest",
    "GoalEditRequest",
    "ItemMaterialCreateRequest",
    "ItemMaterialPatchRequest",
    "MaterialMoveRequest",
    "OrderCreateRequest",
    "OrderPatchRequest",
    "PersonCreateRequest",
    "PersonPatchRequest",
    "PipelineMoveRequest",
    "UndoEventsRequest",
    "VendorCreateRequest",
    "VendorPatchRequest",
    "_require_munim_access",
    "_resolve_actor_access",
    "_resolve_actor_person",
    "_validate_goal_id_or_400",
    "add_customer_to_directory",
    "add_item_raw_material",
    "add_person_to_roster",
    "add_vendor_to_directory",
    "agent_core",
    "bulk_update_order_materials_status",
    "choose_agent_goal_option",
    "confirm_agent_goal",
    "create_order_flat_item",
    "create_production_order",
    "delete_item_raw_material",
    "discard_agent_goal",
    "edit_agent_goal",
    "execute_web_agent_turn",
    "get_media_signed_url",
    "get_munim_ledger",
    "get_munim_overview",
    "get_order_timeline",
    "get_production_overview",
    "get_supabase_admin",
    "list_needs_info_queue",
    "move_order_material_stage",
    "move_order_pipeline_stage",
    "park_agent_goal_for_clerk",
    "patch_customer_in_directory",
    "patch_item_raw_material",
    "patch_person_in_roster",
    "patch_production_order",
    "patch_vendor_in_directory",
    "router",
    "search_archive",
    "step_executors",
    "storage",
    "trigger_zoho_pulse_sync",
    "undo_agent_goal",
    "undo_production_events",
    "update_order_flat_item",
    "zoho",
]
