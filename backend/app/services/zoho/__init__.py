"""Zoho Books India integration package (Standard Plan, GST-Off Management Ledger)."""

from app.config import get_settings
from app.services.zoho.bills import check_duplicate_bill, create_draft_bill, create_draft_invoice
from app.services.zoho.bridge import (
    _extract_zoho_doc_project_keys,
    _find_existing_by_reference,
    ensure_project_zoho_bridge,
)
from app.services.zoho.client import (
    _COA_CACHE,
    _TOKEN_CACHE,
    _get_access_token,
    _get_chart_of_accounts,
    _list_all_zoho_docs,
    _token_cache,
    _zoho_request,
)
from app.services.zoho.contacts import (
    NON_PAYEE_WORDS,
    ROLE_STOPWORDS,
    _upsert_entity_alias,
    create_zoho_contact,
    find_matching_active_orders,
    match_existing_customer,
    match_existing_order_or_customer,
    match_existing_payee,
    merge_notes_text,
    resolve_or_create_contact,
    resolve_payee_zoho_vendor,
    search_zoho_contacts,
    sync_existing_zoho_contact,
    update_zoho_contact_notes,
)
from app.services.zoho.inventory_ledger import (
    _pick_or_create_inventory_asset_account,
    build_fifo_vendor_bill_allocations,
    extract_project_matched_doc_amount,
    push_inventory_journal_to_zoho,
    push_mixed_inventory_load_to_zoho,
)
from app.services.zoho.ledger import (
    _pick_matching_zoho_bank_account,
    _pick_zoho_expense_account,
    _post_zoho_with_order_tag_fallback,
    _resolve_zoho_paid_account_id,
    push_management_entry_to_zoho,
)
from app.services.zoho.mutations import delete_zoho_financial_entry
from app.services.zoho.sync import (
    list_recent_ledger_transactions,
    sync_owner_pulse_snapshot,
    sync_project_financial_caches,
)

__all__ = [
    "_COA_CACHE",
    "_TOKEN_CACHE",
    "NON_PAYEE_WORDS",
    "ROLE_STOPWORDS",
    "_extract_zoho_doc_project_keys",
    "_find_existing_by_reference",
    "_get_access_token",
    "_get_chart_of_accounts",
    "_list_all_zoho_docs",
    "_pick_matching_zoho_bank_account",
    "_pick_or_create_inventory_asset_account",
    "_pick_zoho_expense_account",
    "_post_zoho_with_order_tag_fallback",
    "_resolve_zoho_paid_account_id",
    "_token_cache",
    "_upsert_entity_alias",
    "_zoho_request",
    "build_fifo_vendor_bill_allocations",
    "check_duplicate_bill",
    "create_draft_bill",
    "create_draft_invoice",
    "create_zoho_contact",
    "delete_zoho_financial_entry",
    "ensure_project_zoho_bridge",
    "extract_project_matched_doc_amount",
    "find_matching_active_orders",
    "get_settings",
    "list_recent_ledger_transactions",
    "match_existing_customer",
    "match_existing_order_or_customer",
    "match_existing_payee",
    "merge_notes_text",
    "push_inventory_journal_to_zoho",
    "push_management_entry_to_zoho",
    "push_mixed_inventory_load_to_zoho",
    "resolve_or_create_contact",
    "resolve_payee_zoho_vendor",
    "search_zoho_contacts",
    "sync_existing_zoho_contact",
    "sync_owner_pulse_snapshot",
    "sync_project_financial_caches",
    "update_zoho_contact_notes",
]
