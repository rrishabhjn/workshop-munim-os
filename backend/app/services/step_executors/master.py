"""Master Data Spine & Order Metadata CRUD + Cascades + Soft-Archive + Undo."""

from __future__ import annotations

from typing import Any

from app.services import zoho
from app.services.goal_primitives import format_inr, merge_unique_str_list
from app.services.step_executors.common import _format_customer_order_display_name, sanitize_iso_date
from app.services.step_executors.production import transition_project_pipeline


def create_standalone_customer(
    sb: Any,
    *,
    full_name: str,
    area_landmark: str | None = None,
    phone: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a Customer record in the Master Spine without creating a Lead order or Zoho clutter."""
    cname = full_name.strip()
    if not cname:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer full name is required."}}
    payload: dict[str, Any] = {
        "full_name": cname,
        "area_landmark": (area_landmark or "").strip() or None,
        "phone": (phone or "").strip() or None,
        "aliases": merge_unique_str_list([], aliases),
        "notes": (notes or "").strip() or None,
    }
    ins = sb.table("customers").insert(payload).execute().data or []
    cust = ins[0]
    area_txt = f" ({cust['area_landmark']})" if cust.get("area_landmark") else ""
    return {
        "ok": True,
        "customer_id": cust["id"],
        "customer": cust,
        "receipt": f"Added Customer *{cust['full_name']}*{area_txt}",
    }


async def update_customer_record(
    sb: Any,
    *,
    customer_id: str,
    full_name: str | None = None,
    area_landmark: str | None = None,
    phone: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    replace_lists_and_notes: bool = False,
) -> dict[str, Any]:
    """Update a Customer, cascade name/area/phone to active Orders, and sync Zoho if already linked."""
    rows = sb.table("customers").select("*").eq("id", customer_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Customer not found."}}
    cur = rows[0]

    prior_snapshot = {
        "full_name": cur.get("full_name"),
        "area_landmark": cur.get("area_landmark"),
        "phone": cur.get("phone"),
        "aliases": list(cur.get("aliases") or []),
        "notes": cur.get("notes"),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if full_name is not None and full_name.strip() and full_name.strip() != (cur.get("full_name") or ""):
        patch["full_name"] = full_name.strip()
        changes_desc.append(f"Name → *{patch['full_name']}*")
    if area_landmark is not None and area_landmark.strip() != (cur.get("area_landmark") or ""):
        patch["area_landmark"] = area_landmark.strip() or None
        changes_desc.append(f"Area → *{patch['area_landmark'] or '-'}*")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists_and_notes
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if notes is not None:
        new_notes = (
            (notes.strip() or None)
            if replace_lists_and_notes
            else (zoho.merge_notes_text(cur.get("notes"), notes) or None)
        )
        if new_notes != cur.get("notes"):
            patch["notes"] = new_notes
            changes_desc.append("Notes updated")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "customer_id": customer_id,
            "customer": cur,
            "receipt": f"No changes needed for *{cur['full_name']}*",
        }

    updated_rows = sb.table("customers").update(patch).eq("id", customer_id).execute().data or [cur]
    updated = updated_rows[0]

    # Cascade name / area / phone to active Customer Orders in Production
    cascaded_projects_before: list[dict[str, Any]] = []
    if any(k in patch for k in ("full_name", "area_landmark", "phone")):
        active_orders = (
            sb.table("projects")
            .select("id,customer_name,site_area_landmark,phone")
            .eq("customer_id", customer_id)
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        eff_name = updated.get("full_name") or cur["full_name"]
        eff_area = updated.get("area_landmark")
        order_patch: dict[str, Any] = {
            "customer_name": _format_customer_order_display_name(eff_name, eff_area),
        }
        if "area_landmark" in patch:
            order_patch["site_area_landmark"] = eff_area
        if "phone" in patch:
            order_patch["phone"] = updated.get("phone")

        for ord_row in active_orders:
            cascaded_projects_before.append(
                {
                    "id": ord_row["id"],
                    "patch": {
                        "customer_name": ord_row.get("customer_name"),
                        "site_area_landmark": ord_row.get("site_area_landmark"),
                        "phone": ord_row.get("phone"),
                    },
                }
            )
            sb.table("projects").update(order_patch).eq("id", ord_row["id"]).execute()

    # Sync to Zoho Books India ONLY if zoho_contact_id already exists (preserves lazy creation)
    zoho_cid = updated.get("zoho_contact_id") or cur.get("zoho_contact_id")
    zoho_synced = False
    if zoho_cid and any(k in patch for k in ("full_name", "phone", "notes")):
        zoho_synced = await zoho.sync_existing_zoho_contact(
            zoho_cid,
            name=patch.get("full_name"),
            phone=patch.get("phone") if "phone" in patch else None,
            replace_notes=(patch.get("notes") or "") if (replace_lists_and_notes and "notes" in patch) else None,
            notes_to_merge=notes if (not replace_lists_and_notes and "notes" in patch) else None,
        )

    entity_undo = {
        "table": "customers",
        "record_id": customer_id,
        "prior_snapshot": prior_snapshot,
        "cascaded_projects_before": cascaded_projects_before,
        "zoho_contact_id": zoho_cid if zoho_synced else None,
    }
    z_note = " (+ Zoho synced)" if zoho_synced else ""
    ord_note = f" (+ {len(cascaded_projects_before)} active order(s) updated)" if cascaded_projects_before else ""
    return {
        "ok": True,
        "customer_id": customer_id,
        "customer": updated,
        "entity_undo": entity_undo,
        "receipt": f"Customer *{updated['full_name']}*: {', '.join(changes_desc)}{ord_note}{z_note}",
    }


async def update_vendor_record(
    sb: Any,
    *,
    vendor_id: str,
    canonical_name: str | None = None,
    vendor_kind: str | None = None,
    phone: str | None = None,
    gstin: str | None = None,
    specialty: str | None = None,
    aliases: list[str] | None = None,
    notes: str | None = None,
    is_active: bool | None = None,
    replace_lists_and_notes: bool = False,
) -> dict[str, Any]:
    """Update a Vendor/Contractor and sync to Zoho Books India if zoho_contact_id already exists."""
    rows = sb.table("vendors").select("*").eq("id", vendor_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Vendor not found."}}
    cur = rows[0]

    prior_snapshot = {
        "canonical_name": cur.get("canonical_name"),
        "vendor_kind": cur.get("vendor_kind"),
        "phone": cur.get("phone"),
        "gstin": cur.get("gstin"),
        "specialty": cur.get("specialty"),
        "aliases": list(cur.get("aliases") or []),
        "notes": cur.get("notes"),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if canonical_name is not None and canonical_name.strip() and canonical_name.strip() != (cur.get("canonical_name") or ""):
        patch["canonical_name"] = canonical_name.strip()
        changes_desc.append(f"Name → *{patch['canonical_name']}*")
    if vendor_kind in ("supplier", "contractor") and vendor_kind != cur.get("vendor_kind"):
        patch["vendor_kind"] = vendor_kind
        changes_desc.append(f"Kind → `{vendor_kind}`")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if gstin is not None and gstin.strip().upper() != (cur.get("gstin") or ""):
        patch["gstin"] = gstin.strip().upper() or None
        changes_desc.append(f"GSTIN → `{patch['gstin'] or '-'}`")
    if specialty is not None and specialty.strip() != (cur.get("specialty") or ""):
        patch["specialty"] = specialty.strip() or None
        changes_desc.append(f"Specialty → *{patch['specialty'] or '-'}*")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists_and_notes
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if notes is not None:
        new_notes = (
            (notes.strip() or None)
            if replace_lists_and_notes
            else (zoho.merge_notes_text(cur.get("notes"), notes) or None)
        )
        if new_notes != cur.get("notes"):
            patch["notes"] = new_notes
            changes_desc.append("Notes updated")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "vendor_id": vendor_id,
            "vendor": cur,
            "receipt": f"No changes needed for *{cur['canonical_name']}*",
        }

    updated_rows = sb.table("vendors").update(patch).eq("id", vendor_id).execute().data or [cur]
    updated = updated_rows[0]

    zoho_cid = updated.get("zoho_contact_id") or cur.get("zoho_contact_id")
    zoho_synced = False
    if zoho_cid and any(k in patch for k in ("canonical_name", "phone", "gstin", "notes")):
        zoho_synced = await zoho.sync_existing_zoho_contact(
            zoho_cid,
            name=patch.get("canonical_name"),
            phone=patch.get("phone") if "phone" in patch else None,
            gstin=patch.get("gstin") if "gstin" in patch else None,
            replace_notes=(patch.get("notes") or "") if (replace_lists_and_notes and "notes" in patch) else None,
            notes_to_merge=notes if (not replace_lists_and_notes and "notes" in patch) else None,
        )

    entity_undo = {
        "table": "vendors",
        "record_id": vendor_id,
        "prior_snapshot": prior_snapshot,
        "zoho_contact_id": zoho_cid if zoho_synced else None,
    }
    z_note = " (+ Zoho synced)" if zoho_synced else ""
    return {
        "ok": True,
        "vendor_id": vendor_id,
        "vendor": updated,
        "entity_undo": entity_undo,
        "receipt": f"Vendor *{updated['canonical_name']}*: {', '.join(changes_desc)}{z_note}",
    }


def update_person_record(
    sb: Any,
    *,
    person_id: str,
    full_name: str | None = None,
    person_type: str | None = None,
    role_tags: list[str] | None = None,
    phone: str | None = None,
    email: str | None = None,
    aliases: list[str] | None = None,
    payout_aliases: list[str] | None = None,
    is_active: bool | None = None,
    replace_lists: bool = False,
) -> dict[str, Any]:
    """Update a Person / Mistri record (phone, role_tags, voice aliases, bank payout aliases)."""
    rows = sb.table("people").select("*").eq("id", person_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Person not found."}}
    cur = rows[0]

    prior_snapshot = {
        "full_name": cur.get("full_name"),
        "person_type": cur.get("person_type"),
        "role_tags": list(cur.get("role_tags") or []),
        "phone": cur.get("phone"),
        "email": cur.get("email"),
        "aliases": list(cur.get("aliases") or []),
        "payout_aliases": list(cur.get("payout_aliases") or []),
        "is_active": cur.get("is_active", True),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if full_name is not None and full_name.strip() and full_name.strip() != (cur.get("full_name") or ""):
        patch["full_name"] = full_name.strip()
        changes_desc.append(f"Name → *{patch['full_name']}*")
    if person_type in ("operator", "salaried_karigar", "contractor_mistri") and person_type != cur.get("person_type"):
        patch["person_type"] = person_type
        if person_type == "operator":
            patch["can_login_prod"] = True
            patch["can_login_munim"] = True
        changes_desc.append(f"Type → `{person_type}`")
    if role_tags is not None:
        new_roles = (
            [str(r).strip() for r in role_tags if str(r).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("role_tags"), role_tags)
        )
        if new_roles != list(cur.get("role_tags") or []):
            patch["role_tags"] = new_roles
            changes_desc.append(f"Roles → `{', '.join(new_roles)}`")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if email is not None and email.strip() != (cur.get("email") or ""):
        patch["email"] = email.strip() or None
        changes_desc.append(f"Email → `{patch['email'] or '-'}`")
    if aliases is not None:
        new_aliases = (
            [str(a).strip() for a in aliases if str(a).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("aliases"), aliases)
        )
        if new_aliases != list(cur.get("aliases") or []):
            patch["aliases"] = new_aliases
            changes_desc.append(f"Aliases → _{', '.join(new_aliases) or '-'}_")
    if payout_aliases is not None:
        new_pa = (
            [str(a).strip() for a in payout_aliases if str(a).strip()]
            if replace_lists
            else merge_unique_str_list(cur.get("payout_aliases"), payout_aliases)
        )
        if new_pa != list(cur.get("payout_aliases") or []):
            patch["payout_aliases"] = new_pa
            changes_desc.append(f"Bank Proxy → *{', '.join(new_pa) or '-'}*")
    if is_active is not None and bool(is_active) != bool(cur.get("is_active", True)):
        patch["is_active"] = bool(is_active)
        changes_desc.append("Archived" if not is_active else "Restored")

    if not patch:
        return {
            "ok": True,
            "person_id": person_id,
            "person": cur,
            "receipt": f"No changes needed for *{cur['full_name']}*",
        }

    updated_rows = sb.table("people").update(patch).eq("id", person_id).execute().data or [cur]
    updated = updated_rows[0]
    return {
        "ok": True,
        "person_id": person_id,
        "person": updated,
        "entity_undo": {
            "table": "people",
            "record_id": person_id,
            "prior_snapshot": prior_snapshot,
        },
        "receipt": f"Person *{updated['full_name']}*: {', '.join(changes_desc)}",
    }


def update_order_metadata(
    sb: Any,
    *,
    project_id: str,
    quoted_total: float | None = None,
    promised_delivery_date: str | None = None,
    rough_scope: str | None = None,
    site_area_landmark: str | None = None,
    phone: str | None = None,
    boq_scope_changed: bool | None = None,
    boq_scope_note: str | None = None,
    specs_notes: str | None = None,
    uploaded_gcs_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Update Order metadata (deal value ₹, delivery date, scope, area, phone, BOQ note) with Undo snapshot."""
    rows = sb.table("projects").select("*").eq("id", project_id).limit(1).execute().data or []
    if not rows:
        return {"ok": False, "error_kind": "validation_400", "error": {"message": "Order not found."}}
    cur = rows[0]

    prior_snapshot = {
        "quoted_total": cur.get("quoted_total"),
        "promised_delivery_date": cur.get("promised_delivery_date"),
        "rough_scope": cur.get("rough_scope"),
        "site_area_landmark": cur.get("site_area_landmark"),
        "phone": cur.get("phone"),
        "boq_scope_changed": cur.get("boq_scope_changed"),
        "boq_scope_note": cur.get("boq_scope_note"),
        "payment_stage": cur.get("payment_stage"),
    }

    patch: dict[str, Any] = {}
    changes_desc: list[str] = []

    if quoted_total is not None:
        new_q = round(float(quoted_total), 2)
        if new_q != round(float(cur.get("quoted_total") or 0), 2):
            patch["quoted_total"] = new_q
            changes_desc.append(f"Deal Value → *{format_inr(new_q)}*")
            paid = float(cur.get("paid_cache_inr") or 0)
            if new_q > 0 and paid >= new_q:
                patch["payment_stage"] = "payment_complete"
            elif paid > 0 and cur.get("payment_stage") == "payment_complete" and paid < new_q:
                patch["payment_stage"] = "partial_paid"
    if promised_delivery_date is not None:
        raw_d = str(promised_delivery_date).strip()
        if raw_d == "":
            if cur.get("promised_delivery_date") is not None:
                patch["promised_delivery_date"] = None
                changes_desc.append("Delivery Date → *-*")
        else:
            parsed_d = sanitize_iso_date(raw_d)
            if parsed_d and parsed_d != str(cur.get("promised_delivery_date") or ""):
                patch["promised_delivery_date"] = parsed_d
                changes_desc.append(f"Delivery Date → *{parsed_d}*")
    if rough_scope is not None and rough_scope.strip() != (cur.get("rough_scope") or ""):
        patch["rough_scope"] = rough_scope.strip() or None
        changes_desc.append(f"Scope → _{patch['rough_scope'] or '-'}_")
    if site_area_landmark is not None and site_area_landmark.strip() != (cur.get("site_area_landmark") or ""):
        patch["site_area_landmark"] = site_area_landmark.strip() or None
        changes_desc.append(f"Area → *{patch['site_area_landmark'] or '-'}*")
    if phone is not None and phone.strip() != (cur.get("phone") or ""):
        patch["phone"] = phone.strip() or None
        changes_desc.append(f"Phone → `{patch['phone'] or '-'}`")
    if boq_scope_changed is not None and bool(boq_scope_changed) != bool(cur.get("boq_scope_changed", False)):
        patch["boq_scope_changed"] = bool(boq_scope_changed)
        changes_desc.append(f"BOQ Changed → `{bool(boq_scope_changed)}`")
    if boq_scope_note is not None and boq_scope_note.strip() != (cur.get("boq_scope_note") or ""):
        patch["boq_scope_note"] = boq_scope_note.strip() or None
        changes_desc.append("BOQ Note updated")

    existing_meta = dict(cur.get("metadata") or {})
    meta_updated = False
    if specs_notes is not None and str(specs_notes).strip() and str(specs_notes).strip() != (existing_meta.get("specs_notes") or ""):
        existing_meta["specs_notes"] = str(specs_notes).strip()
        meta_updated = True
        changes_desc.append("Specs updated")
    if uploaded_gcs_paths:
        merged_paths = merge_unique_str_list(existing_meta.get("uploaded_gcs_paths"), uploaded_gcs_paths)
        if merged_paths != list(existing_meta.get("uploaded_gcs_paths") or []):
            existing_meta["uploaded_gcs_paths"] = merged_paths
            meta_updated = True
            changes_desc.append("Attachments linked")
    if meta_updated:
        prior_snapshot["metadata"] = cur.get("metadata") or {}
        patch["metadata"] = existing_meta

    if not patch:
        return {
            "ok": True,
            "project_id": project_id,
            "order": cur,
            "receipt": f"No changes needed on `{cur['project_code']}`",
        }

    updated_rows = sb.table("projects").update(patch).eq("id", project_id).execute().data or [cur]
    updated = updated_rows[0]
    return {
        "ok": True,
        "project_id": project_id,
        "order": updated,
        "entity_undo": {
            "table": "projects",
            "record_id": project_id,
            "prior_snapshot": prior_snapshot,
        },
        "receipt": f"Order `{updated['project_code']}` (*{updated['customer_name']}*): {', '.join(changes_desc)}",
    }


async def archive_master_or_order_record(
    sb: Any,
    *,
    entity_kind: str,  # 'customer' | 'vendor' | 'person' | 'order'
    record_id: str,
    actor_person_id: str | None = None,
    source_channel: str = "web",
    note: str | None = None,
) -> dict[str, Any]:
    """Confirm-gated Soft Archive (never hard SQL DELETE)."""
    if entity_kind == "order":
        return await transition_project_pipeline(
            sb,
            project_id=record_id,
            to_stage="lost",
            actor_person_id=actor_person_id,
            source_channel=source_channel,
            note=note or "Archived / marked Lost",
        )
    if entity_kind == "customer":
        return await update_customer_record(sb, customer_id=record_id, is_active=False)
    if entity_kind == "vendor":
        return await update_vendor_record(sb, vendor_id=record_id, is_active=False)
    if entity_kind == "person":
        return update_person_record(sb, person_id=record_id, is_active=False)
    return {"ok": False, "error_kind": "validation_400", "error": {"message": f"Unknown archive target '{entity_kind}'."}}


async def undo_entity_mutations(sb: Any, entity_undos: list[dict[str, Any]]) -> list[str]:
    """Revert one or more Master Spine / Order metadata snapshots in reverse order."""
    reverted: list[str] = []
    for item in reversed(entity_undos):
        table = item.get("table")
        rec_id = item.get("record_id")
        snap = item.get("prior_snapshot") or {}
        if not table or not rec_id or not snap:
            continue
        sb.table(table).update(snap).eq("id", rec_id).execute()
        for prj_undo in item.get("cascaded_projects_before") or []:
            if prj_undo.get("id") and prj_undo.get("patch"):
                sb.table("projects").update(prj_undo["patch"]).eq("id", prj_undo["id"]).execute()
        z_cid = item.get("zoho_contact_id")
        if z_cid and table in ("customers", "vendors"):
            await zoho.sync_existing_zoho_contact(
                z_cid,
                name=snap.get("full_name") or snap.get("canonical_name"),
                phone=snap.get("phone") or "",
                gstin=snap.get("gstin") if table == "vendors" else None,
                replace_notes=snap.get("notes") or "",
            )
        reverted.append(f"Reverted {table[:-1] if table.endswith('s') else table} update")
    return reverted
