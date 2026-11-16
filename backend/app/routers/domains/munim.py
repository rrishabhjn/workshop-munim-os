"""Domain 2: Munim Finance, Master Directory, Archive & Signed Media Endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import CurrentUser, get_current_user
from .common import (
    CustomerCreateRequest,
    CustomerPatchRequest,
    PersonCreateRequest,
    PersonPatchRequest,
    VendorCreateRequest,
    VendorPatchRequest,
    _pkg,
    _require_munim_access,
)

router = APIRouter()


@router.get("/munim/overview")
async def get_munim_overview(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
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
        "zoho_enabled": pkg.zoho.get_settings().zoho_enabled,
    }


@router.post("/munim/sync-zoho")
async def trigger_zoho_pulse_sync(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _require_munim_access(sb, user)
    pulse = await pkg.zoho.sync_owner_pulse_snapshot(sb)
    project_caches = await pkg.zoho.sync_project_financial_caches(sb)
    return {**pulse, "project_caches": project_caches}


@router.post("/munim/people")
async def add_person_to_roster(
    body: PersonCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    sb = _pkg().get_supabase_admin()
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    res = pkg.step_executors.update_person_record(
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
    sb = _pkg().get_supabase_admin()
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _require_munim_access(sb, user)
    res = await pkg.step_executors.update_vendor_record(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _require_munim_access(sb, user)
    res = pkg.step_executors.create_standalone_customer(
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
    pkg = _pkg()
    sb = pkg.get_supabase_admin()
    _require_munim_access(sb, user)
    res = await pkg.step_executors.update_customer_record(
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


@router.get("/archive")
async def search_archive(
    q: str = Query(default=""),
    _: CurrentUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    sb = _pkg().get_supabase_admin()
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
    url = _pkg().storage.generate_signed_url(path, expiration_minutes=30)
    return {"url": url}
