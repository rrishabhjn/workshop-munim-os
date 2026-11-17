"""Zoho Books Standard Plan 1:1 Project Reporting Tag & Custom Field Smart Auto-Bridge."""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    return sys.modules[__package__]


def _extract_zoho_doc_project_keys(
    doc: dict[str, Any], cf_api_name: str = "cf_project_code"
) -> tuple[set[str], set[str], str]:
    """Extract (tag_option_ids, lowercase project codes, legacy zoho_project_id) from a Zoho doc."""
    tag_opt_ids: set[str] = set()
    codes: set[str] = set()

    for tag_entry in [*(doc.get("tags") or []), *(
        t for li in (doc.get("line_items") or []) for t in (li.get("tags") or [])
    )]:
        if isinstance(tag_entry, dict):
            if tag_entry.get("tag_option_id"):
                tag_opt_ids.add(str(tag_entry["tag_option_id"]).strip())
            opt_name = str(tag_entry.get("tag_option_name") or "").strip()
            if opt_name:
                codes.add(opt_name.split(" - ", 1)[0].strip().lower())

    for cf in doc.get("custom_fields") or []:
        if isinstance(cf, dict):
            api_n = str(cf.get("api_name") or cf.get("customfield_id") or "").strip()
            val = str(cf.get("value") or "").strip()
            if val and (api_n == cf_api_name or api_n == "cf_project_code"):
                codes.add(val.lower())

    cf_hash = doc.get("custom_field_hash") or {}
    if isinstance(cf_hash, dict):
        cf_val = cf_hash.get(cf_api_name) or cf_hash.get(f"{cf_api_name}_unformatted") or cf_hash.get("cf_project_code")
        if cf_val:
            codes.add(str(cf_val).strip().lower())

    zp_id = str(doc.get("project_id") or "").strip()
    return tag_opt_ids, codes, zp_id


async def _find_existing_by_reference(
    client: httpx.AsyncClient,
    list_path: str,
    list_key: str,
    id_field: str,
    reference_number: str,
) -> str | None:
    """Query Zoho by reference_number before retrying so timeouts never double-post."""
    if not reference_number:
        return None
    pkg = _pkg()
    try:
        resp = await pkg._zoho_request(
            client, "GET", list_path, params={"reference_number": reference_number}
        )
        if not resp.is_success:
            return None
        rows = resp.json().get(list_key, []) or []
        for row in rows:
            if (row.get("reference_number") or "").strip() == reference_number:
                return str(row.get(id_field) or "")
    except httpx.HTTPError:
        return None
    return None


async def ensure_project_zoho_bridge(
    sb: Any, project_id: str, *, ensure_contact: bool = False
) -> dict[str, Any]:
    """Smart Auto-Bridge on Zoho Standard Plan:
    Ensures the 1:1 Zoho 'Project' Reporting Tag option exists when an order converts,
    and optionally ensures a Zoho Customer Contact (for explicit AR Draft Invoices/Payments).
    """
    pkg = _pkg()
    prj_rows = (
        sb.table("projects")
        .select("id,project_code,customer_id,customer_name,phone,zoho_contact_id,zoho_tag_id,zoho_tag_option_id,cf_project_code")
        .eq("id", project_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not prj_rows:
        return {"ok": False, "error": "Project not found in Supabase."}
    prj = prj_rows[0]

    settings = pkg.get_settings()
    if not settings.zoho_enabled:
        return {
            "ok": True,
            "stubbed": True,
            "zoho_contact_id": prj.get("zoho_contact_id"),
            "zoho_tag_id": prj.get("zoho_tag_id"),
            "zoho_tag_option_id": prj.get("zoho_tag_option_id"),
            "cf_project_code": prj.get("cf_project_code") or prj["project_code"],
        }

    zoho_contact_id = prj.get("zoho_contact_id")
    cust_row: dict[str, Any] | None = None
    if prj.get("customer_id"):
        c_data = (
            sb.table("customers")
            .select("id,full_name,area_landmark,phone,zoho_contact_id")
            .eq("id", prj["customer_id"])
            .limit(1)
            .execute()
            .data
        )
        cust_row = c_data[0] if c_data else None
        if cust_row and cust_row.get("zoho_contact_id"):
            zoho_contact_id = cust_row["zoho_contact_id"]

    # 1. Ensure Zoho Customer Contact only when explicitly requested (Draft Invoice / AR Payment)
    if ensure_contact and not zoho_contact_id:
        display_name = prj["customer_name"]
        if cust_row and cust_row.get("area_landmark"):
            display_name = f"{cust_row['full_name']} ({cust_row['area_landmark']})"
        c_res = await pkg.resolve_or_create_contact(
            sb,
            display_name,
            "customer",
            phone=(cust_row or {}).get("phone") or prj.get("phone"),
        )
        if c_res.get("contact_id"):
            zoho_contact_id = c_res["contact_id"]
            if cust_row:
                sb.table("customers").update({"zoho_contact_id": zoho_contact_id}).eq(
                    "id", cust_row["id"]
                ).execute()

    # 2. Ensure Zoho Reporting Tag Option (Standard Plan replacement for Zoho Projects)
    tag_id = prj.get("zoho_tag_id") or settings.zoho_project_tag_id or None
    tag_option_id = prj.get("zoho_tag_option_id")
    option_label = f"{prj['project_code']} - {prj['customer_name']}"[:95]

    if not tag_option_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if not tag_id:
                    tags_resp = await pkg._zoho_request(client, "GET", "/reportingtags")
                    if tags_resp.is_success:
                        tags_json = tags_resp.json()
                        all_tags = tags_json.get("reporting_tags") or tags_json.get("tags") or []
                        project_tag = next(
                            (
                                t
                                for t in all_tags
                                if t.get("is_active", True)
                                and "project" in (t.get("tag_name") or "").lower()
                            ),
                            None,
                        )
                        if project_tag and project_tag.get("tag_id"):
                            tag_id = str(project_tag["tag_id"])

                if tag_id:
                    tag_resp = await pkg._zoho_request(client, "GET", f"/reportingtags/{tag_id}")
                    opts: list[dict[str, Any]] = []
                    if tag_resp.is_success:
                        tag_json = tag_resp.json()
                        tag_obj = tag_json.get("reporting_tag") or tag_json.get("tag") or tag_json
                        opts = list(tag_obj.get("tag_options") or tag_json.get("tag_options") or [])
                    if not opts:
                        opts_resp = await pkg._zoho_request(client, "GET", f"/reportingtags/{tag_id}/options/all")
                        if opts_resp.is_success:
                            opts = list(opts_resp.json().get("tag_options") or [])

                    existing_opt = next(
                        (
                            o
                            for o in opts
                            if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                        ),
                        None,
                    )
                    if existing_opt:
                        tag_option_id = str(existing_opt.get("tag_option_id") or "") or None
                    else:
                        add_resp = await pkg._zoho_request(
                            client,
                            "POST",
                            f"/reportingtags/{tag_id}/options",
                            json_body={"tag_option_name": option_label, "is_active": True},
                        )
                        if not add_resp.is_success:
                            merged_opts = [
                                {
                                    "tag_option_id": o.get("tag_option_id"),
                                    "tag_option_name": o.get("tag_option_name"),
                                    "is_active": o.get("is_active", True),
                                }
                                for o in opts
                                if o.get("tag_option_name")
                            ] + [{"tag_option_name": option_label, "is_active": True}]
                            add_resp = await pkg._zoho_request(
                                client,
                                "PUT",
                                f"/reportingtags/{tag_id}/options",
                                json_body={"tag_options": merged_opts},
                            )
                        if add_resp.is_success:
                            add_json = add_resp.json()
                            created_opt = add_json.get("tag_option")
                            if not created_opt:
                                resp_opts = add_json.get("tag_options") or []
                                created_opt = next(
                                    (
                                        o
                                        for o in resp_opts
                                        if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                                    ),
                                    resp_opts[0] if resp_opts else {},
                                )
                            tag_option_id = str((created_opt or {}).get("tag_option_id") or "") or None
                            if not tag_option_id:
                                ref_resp = await pkg._zoho_request(client, "GET", f"/reportingtags/{tag_id}")
                                if ref_resp.is_success:
                                    r_json = ref_resp.json()
                                    r_obj = r_json.get("reporting_tag") or r_json.get("tag") or r_json
                                    r_opts = r_obj.get("tag_options") or r_json.get("tag_options") or []
                                    matched = next(
                                        (
                                            o
                                            for o in r_opts
                                            if prj["project_code"].lower() in (o.get("tag_option_name") or "").lower()
                                        ),
                                        None,
                                    )
                                    if matched:
                                        tag_option_id = str(matched.get("tag_option_id") or "") or None
        except httpx.HTTPError as exc:
            logger.warning("Zoho Reporting Tag option auto-create skipped: %s", exc)

    patch: dict[str, Any] = {
        "cf_project_code": prj.get("cf_project_code") or prj["project_code"],
    }
    if zoho_contact_id:
        patch["zoho_contact_id"] = zoho_contact_id
    if tag_id:
        patch["zoho_tag_id"] = tag_id
    if tag_option_id:
        patch["zoho_tag_option_id"] = tag_option_id
    sb.table("projects").update(patch).eq("id", project_id).execute()

    return {
        "ok": True,
        "stubbed": False,
        "zoho_contact_id": zoho_contact_id,
        "zoho_tag_id": tag_id,
        "zoho_tag_option_id": tag_option_id,
        "cf_project_code": patch["cf_project_code"],
    }
