"""Guarded, Idempotent Step Executors + 1-Tap [↩️ Undo] + Anti-Spam Handoff Pings."""

from app.services import telegram_api, zoho
from app.services.step_executors.common import (
    _ITEM_STAGE_SYNONYMS,
    _MATERIAL_TRACK_SYNONYMS,
    _PIPELINE_STAGE_SYNONYMS,
    _compute_duration_hours,
    _format_customer_order_display_name,
    _norm_phone10,
    _notify_operator_handoff,
    _parse_iso_dt,
    get_default_person_id,
    normalize_stage_code_for_op,
    parse_positive_quantity,
    sanitize_iso_date,
)
from app.services.step_executors.master import (
    archive_master_or_order_record,
    create_standalone_customer,
    undo_entity_mutations,
    update_customer_record,
    update_order_metadata,
    update_person_record,
    update_vendor_record,
)
from app.services.step_executors.materials import (
    MATERIAL_STATUS_LABELS,
    add_item_materials_batch,
    delete_item_material_row,
    infer_material_category,
    normalize_material_status,
    sync_order_material_track_from_items,
    update_item_materials_rows,
)
from app.services.step_executors.munim import (
    execute_munim_draft_doc_step,
    execute_munim_financial_step,
)
from app.services.step_executors.production import (
    create_customer_and_lead,
    enrich_whos_doing_what_rows,
    transition_material_track,
    transition_project_pipeline,
    undo_stage_events,
    upsert_or_transition_item,
)

__all__ = [
    "MATERIAL_STATUS_LABELS",
    "add_item_materials_batch",
    "archive_master_or_order_record",
    "create_customer_and_lead",
    "create_standalone_customer",
    "delete_item_material_row",
    "enrich_whos_doing_what_rows",
    "execute_munim_draft_doc_step",
    "execute_munim_financial_step",
    "get_default_person_id",
    "infer_material_category",
    "normalize_material_status",
    "normalize_stage_code_for_op",
    "parse_positive_quantity",
    "sanitize_iso_date",
    "sync_order_material_track_from_items",
    "telegram_api",
    "transition_material_track",
    "transition_project_pipeline",
    "undo_entity_mutations",
    "undo_stage_events",
    "update_customer_record",
    "update_item_materials_rows",
    "update_order_metadata",
    "update_person_record",
    "update_vendor_record",
    "upsert_or_transition_item",
    "zoho",
]
