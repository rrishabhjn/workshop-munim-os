"""Architectural guardrail contracts for refactored backend packages and pruned Wave-0 code."""

from __future__ import annotations

from pathlib import Path

from app.main import app
from app.routers import domains
from app.services import agent_core, gemini_router, step_executors, zoho

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fastapi_route_inventory_and_wave0_removal():
    """Verify every active v1 endpoint exists and removed Wave-0 routes stay absent."""
    paths = set(app.openapi()["paths"].keys())

    expected_active = {
        "/health",
        "/api/public-config",
        "/api/archive",
        "/api/media/signed-url",
        "/api/production/overview",
        "/api/production/orders",
        "/api/production/orders/{project_id}",
        "/api/production/orders/{project_id}/pipeline",
        "/api/production/orders/{project_id}/material",
        "/api/production/orders/{project_id}/items",
        "/api/production/items/{item_id}",
        "/api/production/items/{item_id}/materials",
        "/api/production/materials/{material_id}",
        "/api/production/orders/{project_id}/materials/bulk-status",
        "/api/production/undo",
        "/api/production/orders/{project_id}/timeline",
        "/api/munim/overview",
        "/api/munim/ledger",
        "/api/munim/sync-zoho",
        "/api/munim/people",
        "/api/munim/people/{person_id}",
        "/api/munim/vendors",
        "/api/munim/vendors/{vendor_id}",
        "/api/munim/customers",
        "/api/munim/customers/{customer_id}",
        "/api/agent/needs-info",
        "/api/agent/turn",
        "/api/agent/goals/{goal_id}/confirm",
        "/api/agent/goals/{goal_id}/choice",
        "/api/agent/goals/{goal_id}/undo",
        "/api/agent/goals/{goal_id}/park",
        "/api/agent/goals/{goal_id}/discard",
        "/api/agent/goals/{goal_id}/edit",
        "/telegram/webhook",
        "/tasks/telegram-worker",
        "/tasks/goal-retry",
        "/cron/morning-brief",
        "/cron/nightly-sync",
    }
    missing = expected_active - paths
    assert not missing, f"Missing expected active routes: {sorted(missing)}"

    removed_wave0 = {
        "/api/cash-ledger",
        "/api/followups",
        "/api/entities",
        "/api/stages",
    }
    resurfaced = removed_wave0 & paths
    assert not resurfaced, f"Wave-0 routes should remain deleted: {sorted(resurfaced)}"


def test_facade_reexports_and_mock_patch_targets():
    """Verify subpackage facades preserve every public & `mock.patch`-targeted attribute."""
    # Batch OCR script contract
    for attr in ("extract_supplier_bill", "SupplierBillExtraction", "BillLineItem"):
        assert hasattr(gemini_router, attr), f"gemini_router missing {attr}"

    # Pruned dead Wave-0 symbols & deleted monolithic/JSX files stay gone
    assert not hasattr(zoho, "sync_cash_entry_to_zoho")
    deleted_paths = [
        REPO_ROOT / "backend" / "app" / "routers" / "dashboard.py",
        REPO_ROOT / "backend" / "app" / "routers" / "domains.py",
        REPO_ROOT / "backend" / "app" / "services" / "agent_core.py",
        REPO_ROOT / "backend" / "app" / "services" / "step_executors.py",
        REPO_ROOT / "backend" / "app" / "services" / "zoho.py",
        REPO_ROOT / "frontend" / "src" / "components" / "BackOffice.jsx",
        REPO_ROOT / "frontend" / "src" / "components" / "KanbanBoard.jsx",
        REPO_ROOT / "frontend" / "src" / "components" / "ProjectDetail.jsx",
        REPO_ROOT / "frontend" / "src" / "components" / "ProjectsBoard.jsx",
        REPO_ROOT / "frontend" / "src" / "components" / "PurchaseList.jsx",
    ]
    for p in deleted_paths:
        assert not p.exists(), f"Dead/legacy file still present on disk: {p}"

    # Patch & public contracts across facades
    for attr in (
        "get_settings",
        "_get_interactions_client",
        "_run_single_step",
        "confirm_and_execute_goal",
        "run_agent_turn",
        "apply_slot_patch_and_resume",
        "edit_open_goal_slots",
        "undo_completed_goal",
        "park_goal_for_clerk",
        "discard_goal",
        "AgentTurnEnvelope",
        "GoalIntentItem",
        "DisambiguationChoice",
        "ErrorRepairProposal",
        "_inline_json_schema",
        "_process_goal_intent_item",
        "_build_compact_grounding",
        "_build_system_instruction",
    ):
        assert hasattr(agent_core, attr), f"agent_core facade missing {attr}"

    for attr in (
        "get_settings",
        "_get_access_token",
        "_zoho_request",
        "_token_cache",
        "check_duplicate_bill",
        "search_zoho_contacts",
        "create_zoho_contact",
        "_upsert_entity_alias",
        "resolve_or_create_contact",
        "create_draft_bill",
        "sync_existing_zoho_contact",
        "ensure_project_zoho_bridge",
        "push_management_entry_to_zoho",
        "sync_owner_pulse_snapshot",
        "sync_project_financial_caches",
    ):
        assert hasattr(zoho, attr), f"zoho facade missing {attr}"

    for attr in (
        "enrich_whos_doing_what_rows",
        "create_customer_and_lead",
        "update_order_metadata",
        "transition_project_pipeline",
        "transition_material_track",
        "upsert_or_transition_item",
        "add_item_materials_batch",
        "update_item_materials_rows",
        "delete_item_material_row",
        "undo_stage_events",
        "update_person_record",
        "update_vendor_record",
        "create_standalone_customer",
        "update_customer_record",
    ):
        assert hasattr(step_executors, attr), f"step_executors facade missing {attr}"

    for attr in ("router", "get_supabase_admin", "agent_core", "step_executors", "storage", "zoho"):
        assert hasattr(domains, attr), f"domains facade missing {attr}"


def test_python_module_line_count_guardrails():
    """Keep refactored modules & test suites small (<900 lines; <950 ceiling repo-wide)."""
    strict_dirs = [
        REPO_ROOT / "backend" / "tests",
        REPO_ROOT / "backend" / "app" / "routers",
        REPO_ROOT / "backend" / "app" / "services" / "agent_core",
        REPO_ROOT / "backend" / "app" / "services" / "step_executors",
        REPO_ROOT / "backend" / "app" / "services" / "zoho",
    ]
    for folder in strict_dirs:
        for py_file in folder.rglob("*.py"):
            line_count = len(py_file.read_text(encoding="utf-8").splitlines())
            assert line_count < 900, f"{py_file} grew to {line_count} lines (>=900)"

    for folder in (REPO_ROOT / "backend" / "app", REPO_ROOT / "backend" / "tests"):
        for py_file in folder.rglob("*.py"):
            line_count = len(py_file.read_text(encoding="utf-8").splitlines())
            assert line_count < 950, f"{py_file} grew to {line_count} lines (>=950)"
