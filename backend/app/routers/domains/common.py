"""Shared helpers and Pydantic request schemas for `app.routers.domains`."""

from __future__ import annotations

import sys
from typing import Any
import uuid

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.auth import CurrentUser


def _pkg() -> Any:
    """Resolve the facade package dynamically so `unittest.mock.patch` seams stay live."""
    return sys.modules[__package__]


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
    return None, user.display_name, False


def _filter_needs_info_rows(
    rows: list[dict[str, Any]], *, can_munim: bool
) -> list[dict[str, Any]]:
    """Hide Munim finance clarifications/errors from Production-only operators."""
    if can_munim:
        return rows
    return [r for r in rows if r.get("domain_scope") != "munim"]


def _enforce_goal_munim_rbac(
    sb: Any, user: CurrentUser, goal_id: str
) -> tuple[str | None, str, bool]:
    """Block Production-only operators from reading or acting on Munim finance goals."""
    actor_id, actor_name, can_munim = _resolve_actor_access(sb, user)
    if not can_munim:
        rows = (
            sb.table("agent_goals")
            .select("domain_scope")
            .eq("id", goal_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if rows and rows[0].get("domain_scope") == "munim":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Munim finance access required for this goal.",
            )
    return actor_id, actor_name, can_munim


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


def _validate_goal_id_or_400(sb: Any, goal_id: str) -> str:
    if hasattr(sb, "store"):
        return goal_id
    try:
        return str(uuid.UUID(str(goal_id).strip()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid goal ID.") from exc


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
    paid_cache_inr: float | None = None
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


class GoalReplyRequest(BaseModel):
    text: str = Field(min_length=1)
