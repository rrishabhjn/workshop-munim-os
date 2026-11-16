"""Tests for Production lifecycle, materials CRUD, adversarial sanitization, dedup, and RBAC."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.auth import CurrentUser, get_current_user
from app.config import Settings, get_settings
from app.main import app
from app.services import agent_core, goal_intelligence, step_executors
from app.services.agent_core import AgentTurnEnvelope
from conftest import FakeQuery, FakeSupabase, FakeTable


@pytest.mark.asyncio
async def test_parked_hardening_schemas_callback_dedup_and_prompt_guard():
    # 1. DisambiguationChoice.slot_patch_json + ErrorRepairProposal.repair_options required
    repair_schema = agent_core._inline_json_schema(
        agent_core.ErrorRepairProposal, force_required=("repair_options",)
    )
    assert "repair_options" in repair_schema.get("required", [])
    repair_item = repair_schema["properties"]["repair_options"]["items"]
    assert "slot_patch_json" in repair_item.get("required", [])

    env_schema = agent_core._inline_json_schema(
        AgentTurnEnvelope, force_required=("goal_items",)
    )
    choice_schema = (
        env_schema["properties"]["goal_items"]["items"]["properties"]["choices"]["items"]
    )
    assert "slot_patch_json" in choice_schema.get("required", [])

    # 2. DistilledMemoryArtifact schema flattens nullable anyOf & marks key fields required
    mem_schema = goal_intelligence._inline_schema(
        goal_intelligence.DistilledMemoryArtifact,
        force_required=("should_save", "rule_text", "category"),
    )
    assert {"should_save", "rule_text", "category"}.issubset(
        set(mem_schema.get("required", []))
    )
    assert "anyOf" not in json.dumps(mem_schema)

    # 3. active_orders_index > 35 guardrail in system instruction
    sys_inst = agent_core._build_system_instruction("{}")
    assert "35" in sys_inst
    assert "search_projects_and_items" in sys_inst

    # 4. Duplicate Telegram callback_query update_id is deduplicated cleanly
    class _DedupInsertQuery(FakeQuery):
        def execute(self) -> SimpleNamespace:
            if self._op == "insert":
                upd_id = self._payload.get("telegram_update_id")
                if any(r.get("telegram_update_id") == upd_id for r in self._rows):
                    raise RuntimeError("duplicate key value violates unique constraint (23505)")
            return super().execute()

    class _DedupTable(FakeTable):
        def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> FakeQuery:
            return _DedupInsertQuery(self.rows).insert(payload)

    class _DedupSupabase(FakeSupabase):
        def table(self, name: str) -> FakeTable:
            rows = self.store.setdefault(name, [])
            return _DedupTable(rows) if name == "telegram_webhook_dedup" else FakeTable(rows)

    dedup_sb = _DedupSupabase({})
    cb_mock = AsyncMock(return_value={"ok": True})
    cb_payload = {
        "update_id": 88001,
        "callback_query": {
            "id": "cq-dedup-1",
            "from": {"id": 1001, "first_name": "Shreyansh"},
            "message": {"message_id": 55, "chat": {"id": 1001, "type": "private"}},
            "data": "ag_confirm:goal-1",
        },
    }

    try:
        app.dependency_overrides[get_settings] = lambda: Settings(
            telegram_webhook_secret="sec-cb",
            allowed_telegram_user_ids="1001",
        )
        with (
            patch("app.routers.telegram.get_supabase_admin", return_value=dedup_sb),
            patch("app.routers.telegram._handle_agent_callback", cb_mock),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                first = await client.post(
                    "/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "sec-cb"},
                    json=cb_payload,
                )
                second = await client.post(
                    "/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "sec-cb"},
                    json=cb_payload,
                )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == {"ok": True, "deduped": True}
        assert cb_mock.await_count == 1
    finally:
        app.dependency_overrides.clear()


def test_agent_envelope_sanitizes_literal_null_strings():
    raw = json.dumps(
        {
            "heard_transcript": "null",
            "intent_category": "goal_actions",
            "direct_reply_text": "null",
            "referenced_project_id": "null",
            "referenced_customer_id": "None",
            "goal_items": [
                {
                    "target_open_goal_id": "null",
                    "operation_kind": "customer_receipt",
                    "summary_title": "Praveen Mehta ₹20,000 Cash Advance",
                    "project_id": "7923bc50-77dc-4779-9a73-61cf722c318e",
                    "customer_id": "null",
                    "estimator_ids": ["null", ""],
                    "amount_inr": 20000,
                    "payment_account_code": "cash_galla",
                    "party_name": "Praveen Mehta",
                }
            ],
        }
    )
    env = AgentTurnEnvelope.model_validate_json(raw)
    assert env.heard_transcript is None
    assert env.direct_reply_text is None
    assert env.referenced_project_id is None
    assert env.referenced_customer_id is None
    item = env.goal_items[0]
    assert item.target_open_goal_id is None
    assert item.customer_id is None
    assert item.estimator_ids == []
    assert item.project_id == "7923bc50-77dc-4779-9a73-61cf722c318e"


@pytest.mark.asyncio
async def test_item_materials_crud_track_sync_undo_agent_and_rest_api():
    # 1. Category inference & status normalization ('To Order' / 'Ordered' / 'In Ship' -> 'in_shop')
    assert step_executors.infer_material_category("18mm HDHMR Ply") == "ply_wood"
    assert step_executors.infer_material_category("Advance 1mm Laminate") == "laminate"
    assert step_executors.infer_material_category("Ebco Soft Close Hinges") == "hardware"
    assert step_executors.infer_material_category("Sleepwell 40D Foam") == "foam_fabric"
    assert step_executors.infer_material_category("Fevicol SWR Adhesive") == "polish"
    assert step_executors.normalize_material_status("To Order") == "needed"
    assert step_executors.normalize_material_status("Ordered") == "ordered"
    assert step_executors.normalize_material_status("In Ship") == "in_shop"
    assert step_executors.normalize_material_status("In Shop") == "in_shop"

    # 2. Direct executor batch add -> auto-sync check_stock -> ordered_pending -> all_in_shop + Undo
    sb = FakeSupabase(
        {
            "people": [
                {"id": "p-shreyansh", "full_name": "Shreyansh", "email": "shreyansh@example.com", "person_type": "operator", "is_active": True},
                {"id": "p-ansh", "full_name": "Ansh", "person_type": "operator", "is_default_purchase": True, "is_active": True},
            ],
            "projects": [
                {
                    "id": "prj-1",
                    "project_code": "PRJ-OCT-007",
                    "customer_name": "Samiksha Mam",
                    "project_type": "customer",
                    "pipeline_stage": "execution",
                    "material_stage": "not_started",
                    "status": "active",
                }
            ],
            "project_items": [
                {"id": "it-1", "project_id": "prj-1", "item_name": "Sofa", "stage": "in_shop"},
                {"id": "it-2", "project_id": "prj-1", "item_name": "TV Panel", "stage": "in_shop"},
            ],
            "material_categories": [
                {"code": "ply_wood", "label": "Ply / Wood", "sort_order": 1},
                {"code": "hardware", "label": "Hardware", "sort_order": 3},
            ],
            "item_materials": [],
        }
    )

    add1 = await step_executors.add_item_materials_batch(
        sb,
        project_id="prj-1",
        item_id="it-1",
        materials=[
            {"material_name": "18mm Ply", "quantity": 6, "unit": "sheets"},
            {"material_name": "Sleepwell 40D Foam", "quantity": 4, "unit": "pcs"},
        ],
        actor_person_id="p-shreyansh",
    )
    assert add1["ok"] is True
    assert len(sb.store["item_materials"]) == 2
    assert sb.store["projects"][0]["material_stage"] == "check_stock"

    # Marking 1 row ordered moves project track -> ordered_pending
    m1_id, m2_id = add1["material_ids"]
    upd1 = step_executors.update_item_materials_rows(
        sb,
        material_ids=[m1_id],
        patch={"status": "ordered"},
        actor_person_id="p-ansh",
    )
    assert upd1["ok"] is True
    assert sb.store["projects"][0]["material_stage"] == "ordered_pending"

    # Marking both In Shop moves project track -> all_in_shop
    upd2 = step_executors.update_item_materials_rows(
        sb,
        material_ids=[m1_id, m2_id],
        patch={"status": "in_shop"},
        actor_person_id="p-ansh",
    )
    assert upd2["ok"] is True
    assert sb.store["projects"][0]["material_stage"] == "all_in_shop"

    # Undo upd2 reverts material status & project material_stage back to ordered_pending
    reverted = step_executors.undo_stage_events(sb, upd2["undo_event_ids"], actor_person_id="p-ansh")
    assert reverted
    assert sb.store["projects"][0]["material_stage"] == "ordered_pending"

    # Delete + Undo restores deleted row
    del_res = step_executors.delete_item_material_row(sb, material_id=m2_id, actor_person_id="p-ansh")
    assert del_res["ok"] is True
    assert len(sb.store["item_materials"]) == 1
    step_executors.undo_stage_events(sb, del_res["undo_event_ids"], actor_person_id="p-ansh")
    assert len(sb.store["item_materials"]) == 2

    # 3. AgentCore item_material_upsert (creates missing item + adds material + verifies + undoes)
    item = agent_core.GoalIntentItem(
        operation_kind="item_material_upsert",
        summary_title="Add Hinges to Wardrobe on Samiksha",
        project_id="prj-1",
        item_name="Wardrobe",
        material_lines_json=json.dumps([
            {"material_name": "Ebco Soft Close Hinges", "quantity": 12, "unit": "pcs", "status": "needed"}
        ]),
    )
    out = await agent_core._process_goal_intent_item(
        sb,
        item=item,
        acc_code_to_id={},
        owner_person_id="p-shreyansh",
        actor_label="Shreyansh",
        source_channel="web_command_bar",
        domain_scope="production",
        thinking_level="low",
        heard_transcript=None,
        uploaded_gcs_paths=[],
        ui_context={},
        telegram_chat_id=None,
        user_text="Wardrobe mein 12 Ebco Hinges add karo",
    )
    assert out["status"] == "completed"
    assert out["verified"] is True
    assert out["undo_available"] is True
    assert len(sb.store["project_items"]) == 3
    assert len(sb.store["item_materials"]) == 3

    undo_res = agent_core.undo_completed_goal(sb, goal_id=out["goal_id"], actor_person_id="p-shreyansh")
    assert undo_res["ok"] is True
    assert len(sb.store["project_items"]) == 2
    assert len(sb.store["item_materials"]) == 2

    # 4. REST Endpoints + /api/production/overview nesting
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="supabase", identifier="shreyansh@example.com", display_name="Shreyansh"
    )
    try:
        with patch("app.routers.domains.get_supabase_admin", return_value=sb):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r_add = await client.post(
                    "/api/production/items/it-2/materials",
                    json={"material_name": "18mm Ply", "quantity": 4, "unit": "sheets"},
                )
                assert r_add.status_code == 200
                new_mid = r_add.json()["material_ids"][0]

                r_patch = await client.patch(
                    f"/api/production/materials/{new_mid}",
                    json={"status": "ordered"},
                )
                assert r_patch.status_code == 200

                r_bulk = await client.post(
                    "/api/production/orders/prj-1/materials/bulk-status",
                    json={"material_ids": [m1_id, new_mid], "status": "in_shop"},
                )
                assert r_bulk.status_code == 200

                r_ov = await client.get("/api/production/overview")
                assert r_ov.status_code == 200
                ov = r_ov.json()
                assert len(ov["lookups"]["material_categories"]) == 2
                prj_ov = ov["orders"][0]
                assert len(prj_ov["item_materials"]) == 3
                tv_item = next(i for i in prj_ov["items"] if i["id"] == "it-2")
                assert len(tv_item["materials"]) == 1

                r_del = await client.delete(f"/api/production/materials/{new_mid}")
                assert r_del.status_code == 200
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_ai_text_update_adversarial_null_uuid_typecast_and_fallback_hardening():
    import uuid as _uuid

    UUID_COLS = {
        "projects": {"id", "customer_id", "designed_by_id", "sizing_mistri_id", "material_owner_id"},
        "project_items": {"id", "project_id", "production_mistri_id", "assembly_mistri_id", "qc_passed_by_id"},
        "item_materials": {"id", "project_id", "item_id"},
        "customers": {"id"},
        "vendors": {"id"},
        "people": {"id", "linked_vendor_id"},
        "stage_events": {"id", "project_id", "item_id", "actor_person_id", "undone_by_person_id"},
        "agent_goals": {"id", "owner_person_id", "project_id", "customer_id"},
    }
    UUID_LIST_COLS = {
        "projects": {"estimator_ids", "current_owner_ids"},
        "stage_events": {"assigned_people_ids", "undo_event_ids"},
        "agent_goals": {"undo_event_ids"},
    }

    def _assert_uuid(table_name: str, col: str, val: Any) -> None:
        if val is None:
            return
        if col in UUID_COLS.get(table_name, set()):
            try:
                _uuid.UUID(str(val))
            except ValueError as exc:
                raise RuntimeError(f"22P02 invalid input syntax for type uuid: {val!r} ({table_name}.{col})") from exc
        if col in UUID_LIST_COLS.get(table_name, set()) and isinstance(val, list):
            for entry in val:
                try:
                    _uuid.UUID(str(entry))
                except ValueError as exc:
                    raise RuntimeError(f"22P02 invalid input syntax for type uuid[]: {entry!r} ({table_name}.{col})") from exc

    class _StrictQuery(FakeQuery):
        def __init__(self, table_name: str, rows: list[dict[str, Any]]):
            super().__init__(rows)
            self._tname = table_name

        def eq(self, col: str, val: Any) -> "_StrictQuery":
            _assert_uuid(self._tname, col, val)
            return super().eq(col, val)

        def in_(self, col: str, vals: list[Any]) -> "_StrictQuery":
            for v in vals:
                _assert_uuid(self._tname, col, v)
            return super().in_(col, vals)

        def execute(self) -> SimpleNamespace:
            if self._op == "insert":
                items = self._payload if isinstance(self._payload, list) else [self._payload]
                created = []
                for it in items:
                    row = {"id": it.get("id") or str(_uuid.uuid4()), **it}
                    for k, v in row.items():
                        _assert_uuid(self._tname, k, v)
                    self._rows.append(row)
                    created.append(dict(row))
                return SimpleNamespace(data=created)
            if self._op == "update":
                for k, v in (self._payload or {}).items():
                    _assert_uuid(self._tname, k, v)
            return super().execute()

    class _StrictTable(FakeTable):
        def __init__(self, table_name: str, rows: list[dict[str, Any]]):
            super().__init__(rows)
            self._tname = table_name

        def _q(self) -> _StrictQuery:
            return _StrictQuery(self._tname, self.rows)

    class _StrictUuidSupabase(FakeSupabase):
        strict_uuids = True

        def table(self, name: str) -> _StrictTable:
            return _StrictTable(name, self.store.setdefault(name, []))

    pid_samiksha = str(_uuid.uuid4())
    cid_samiksha = str(_uuid.uuid4())
    pid_arvind = str(_uuid.uuid4())
    pid_manish = str(_uuid.uuid4())
    pid_shreyansh = str(_uuid.uuid4())

    sb = _StrictUuidSupabase(
        {
            "people": [
                {"id": pid_shreyansh, "full_name": "Shreyansh", "email": "shreyansh@example.com", "person_type": "operator", "aliases": [], "is_active": True},
                {"id": pid_arvind, "full_name": "Arvind Ji", "person_type": "salaried_karigar", "aliases": ["Arvind", "Arvind Sharma"], "is_active": True},
                {"id": pid_manish, "full_name": "Manish Cushioning", "person_type": "contractor_mistri", "aliases": ["Manish Katiriya", "Manish"], "is_active": True},
            ],
            "customers": [
                {"id": cid_samiksha, "full_name": "Samiksha Mam", "aliases": ["Samiksha"], "is_active": True}
            ],
            "vendors": [],
            "projects": [
                {
                    "id": pid_samiksha,
                    "project_code": "PRJ-OCT-007",
                    "customer_id": cid_samiksha,
                    "customer_name": "Samiksha Mam",
                    "project_type": "customer",
                    "pipeline_stage": "estimating",
                    "material_stage": "not_started",
                    "status": "active",
                }
            ],
            "project_items": [],
            "item_materials": [],
            "stage_events": [],
            "agent_goals": [],
        }
    )

    # 1. Adversarial fenced JSON with formatted numbers, string booleans, comma aliases, list JSON-strings, dates & stage names
    fenced_raw = """```json
{
  "intent_category": "goal_actions",
  "heard_transcript": "null",
  "direct_reply_text": "null",
  "referenced_project_id": "PRJ-OCT-007",
  "referenced_customer_id": "unknown-id",
  "goal_items": [
    {
      "target_open_goal_id": "1",
      "operation_kind": "item_material_upsert",
      "summary_title": "Add 8 sheets 18mm HDHMR ply to Sofa on Samiksha",
      "project_id": "PRJ-OCT-007",
      "item_id": "Sofa",
      "production_mistri_id": "Manish Katiriya",
      "vendor_id": "Doorset",
      "estimator_ids": ["Arvind", "unknown_person"],
      "amount_inr": "₹20,000",
      "quoted_total": "4,50,000",
      "boq_scope_changed": "true",
      "is_ambiguous": "null",
      "aliases": "Alias One, Alias Two",
      "material_status": "In Ship",
      "material_lines_json": [{"material_name": "18mm HDHMR Ply", "quantity": "8 sheets"}],
      "promised_delivery_date": "25/11/2026"
    }
  ]
}
```"""
    env = AgentTurnEnvelope.model_validate_json(agent_core._strip_json_fences(fenced_raw))
    it = env.goal_items[0]
    assert it.amount_inr == 20000.0
    assert it.quoted_total == 450000.0
    assert it.boq_scope_changed is True
    assert it.is_ambiguous is False
    assert it.aliases == ["Alias One", "Alias Two"]
    assert isinstance(it.material_lines_json, str)

    out = await agent_core._process_goal_intent_item(
        sb,
        item=it,
        acc_code_to_id={},
        owner_person_id=pid_shreyansh,
        actor_label="Shreyansh",
        source_channel="web_command_bar",
        domain_scope="production",
        thinking_level="low",
        heard_transcript=None,
        uploaded_gcs_paths=[],
        ui_context={},
        telegram_chat_id=None,
        user_text="Add 8 sheets 18mm HDHMR ply to Sofa",
    )
    assert out["status"] == "completed"
    assert out["project_id"] == pid_samiksha
    assert len(sb.store["item_materials"]) == 1
    assert sb.store["item_materials"][0]["quantity"] == 8.0
    assert sb.store["item_materials"][0]["unit"] == "sheets"
    assert sb.store["item_materials"][0]["status"] == "in_shop"
    assert out["collected_slots"]["production_mistri_id"] == pid_manish
    assert out["collected_slots"]["estimator_ids"] == [pid_arvind]
    assert out["collected_slots"]["party_name"] == "Doorset"
    assert out["collected_slots"]["promised_delivery_date"] == "2026-11-25"

    # 2. Human stage names normalize cleanly + invalid dates become None
    assert step_executors.normalize_stage_code_for_op("pipeline_stage_move", "Quoted to Customer") == "quoted_awaiting_advance"
    assert step_executors.normalize_stage_code_for_op("item_upsert_or_move", "Packed") == "packed_ready"
    assert step_executors.normalize_stage_code_for_op("material_stage_move", "In Ship") == "all_in_shop"
    assert step_executors.sanitize_iso_date("25/11/2026") == "2026-11-25"
    assert step_executors.sanitize_iso_date("TBD") is None

    # 3. Deterministic _backfill_material_slots_from_text when material_lines_json is omitted
    mat_slots: dict[str, Any] = {
        "operation_kind": "item_material_upsert",
        "project_id": pid_samiksha,
        "item_name": "Sofa",
        "summary_title": "Add ply to Sofa",
    }
    agent_core._backfill_material_slots_from_text(
        mat_slots, user_text="Add 8 sheets 18mm HDHMR ply to Sofa", heard_transcript=None
    )
    parsed_mat = json.loads(mat_slots["material_lines_json"])
    assert parsed_mat[0]["quantity"] == 8.0
    assert parsed_mat[0]["unit"] == "sheets"
    assert "18mm HDHMR ply" in parsed_mat[0]["material_name"]

    # 4. record_query_turn_in_history resolves PRJ code & drops invalid customer UUID on _StrictUuidSupabase
    q_row = goal_intelligence.record_query_turn_in_history(
        sb,
        owner_person_id=pid_shreyansh,
        source_channel="web_command_bar",
        domain_scope="production",
        telegram_chat_id=None,
        ui_context={},
        thinking_level="low",
        user_text="How much is pending for Samiksha?",
        heard_transcript=None,
        reply_text="₹1,80,700 pending.",
        referenced_project_id="PRJ-OCT-007",
        referenced_customer_id="unknown-id",
    )
    assert q_row is not None
    assert q_row["project_id"] == pid_samiksha
    assert q_row["customer_id"] is None

    # 5. Never-500 guard on /api/agent/turn
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="supabase", identifier="shreyansh@example.com", display_name="Shreyansh"
    )
    try:
        with (
            patch("app.routers.domains.get_supabase_admin", return_value=sb),
            patch("app.routers.domains.agent_core.run_agent_turn", side_effect=RuntimeError("boom")),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post("/api/agent/turn", data={"text": "test", "domain_scope": "production"})
        assert r.status_code == 200
        body = r.json()
        assert body["is_error"] is True
        assert "preserved" in body["reply_text"]
    finally:
        app.dependency_overrides.clear()


def test_pass1_customer_dedup_on_repeat_lead():
    sb = FakeSupabase(
        {
            "customers": [],
            "projects": [],
            "stage_events": [],
        }
    )
    first = step_executors.create_customer_and_lead(
        sb,
        full_name="Ramesh Sharma",
        area_landmark="Ajmer Road",
        rough_scope="Modular Kitchen",
        phone="9829011111",
    )
    second = step_executors.create_customer_and_lead(
        sb,
        full_name="ramesh sharma",
        area_landmark="Civil Lines",
        rough_scope="Master Wardrobe",
    )
    assert second["customer_id"] == first["customer_id"]
    assert len(sb.store["customers"]) == 1
    assert len(sb.store["projects"]) == 2
    assert sb.store["projects"][0]["site_area_landmark"] == "Ajmer Road"
    assert sb.store["projects"][1]["site_area_landmark"] == "Civil Lines"

    conflict = step_executors.create_customer_and_lead(
        sb,
        full_name="Ramesh Sharma",
        area_landmark="Vaishali Nagar",
        rough_scope="TV Unit",
        phone="9999900000",
    )
    assert conflict["customer_id"] != first["customer_id"]
    assert len(sb.store["customers"]) == 2


@pytest.mark.asyncio
async def test_pass1_rbac_can_login_munim_and_packed_ready_owner():
    pid_shreyansh = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    pid_harshit = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
    pid_manish = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3"
    sb = FakeSupabase(
        {
            "people": [
                {
                    "id": pid_shreyansh,
                    "full_name": "Shreyansh",
                    "email": "shreyansh@example.com",
                    "is_active": True,
                    "can_login_prod": True,
                    "can_login_munim": True,
                    "is_default_qc": True,
                },
                {
                    "id": pid_harshit,
                    "full_name": "Harshit Bhaiya",
                    "email": "harshit@example.com",
                    "is_active": True,
                    "can_login_prod": True,
                    "can_login_munim": False,
                    "is_default_qc": False,
                },
                {
                    "id": pid_manish,
                    "full_name": "Manish",
                    "is_active": True,
                    "person_type": "salaried_karigar",
                    "is_default_qc": False,
                },
            ],
            "payment_accounts": [],
            "project_pipeline_stages": [],
            "material_track_stages": [],
            "payment_track_stages": [],
            "workshop_stages": [],
            "material_categories": [],
            "customers": [],
            "vendors": [],
            "projects": [],
            "project_items": [
                {"id": "item-packed", "item_name": "Crockery Unit", "stage": "packed_ready", "production_mistri_id": pid_manish}
            ],
            "item_materials": [],
            "v_whos_doing_what": [
                {
                    "work_kind": "item_execution",
                    "project_id": "prj-1",
                    "item_id": "item-packed",
                    "customer_name": "Mehta — Crockery Unit",
                    "stage_code": "packed_ready",
                    "assigned_person_ids": [],
                    "days_in_stage": 1,
                }
            ],
            "v_project_pnl": [],
            "v_needs_info_queue": [],
        }
    )

    enriched = step_executors.enrich_whos_doing_what_rows(sb, sb.store["v_whos_doing_what"])
    assert enriched[0]["assigned_person_ids"] == [pid_shreyansh]

    # Fallback to production_mistri_id when no default QC owner exists
    sb_no_qc = FakeSupabase(
        {
            "people": [{"id": pid_manish, "full_name": "Manish", "is_active": True, "is_default_qc": False}],
            "project_items": sb.store["project_items"],
        }
    )
    enriched_fb = step_executors.enrich_whos_doing_what_rows(sb_no_qc, sb.store["v_whos_doing_what"])
    assert enriched_fb[0]["assigned_person_ids"] == [pid_manish]

    # Migration 007 security invariants check
    mig_007 = (Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "007_packed_ready_queue_owner.sql").read_text(encoding="utf-8")
    assert "WITH (security_invoker = true)" in mig_007
    assert "is_default_qc = TRUE" in mig_007
    assert "SECURITY DEFINER" not in mig_007.upper()
    assert "auth.role()" not in mig_007
    assert "user_metadata" not in mig_007

    # Harshit (can_login_munim=False) allowed on /production, blocked (403) on /munim
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="supabase", identifier="harshit@example.com", display_name="Harshit"
    )
    try:
        with patch("app.routers.domains.get_supabase_admin", return_value=sb):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r_prod = await client.get("/api/production/overview")
                assert r_prod.status_code == 200
                p_body = r_prod.json()
                assert p_body["me"]["can_login_munim"] is False
                assert p_body["whos_doing_what"][0]["assigned_person_ids"] == [pid_shreyansh]

                assert (await client.get("/api/munim/overview")).status_code == 403
                assert (await client.post("/api/munim/sync-zoho")).status_code == 403
                assert (
                    await client.post("/api/munim/vendors", json={"canonical_name": "Test Ply"})
                ).status_code == 403
                assert (
                    await client.post(
                        "/api/munim/customers",
                        json={"full_name": "New Cust", "area_landmark": "Sendra Road"},
                    )
                ).status_code == 403
                r_agent = await client.post("/api/agent/turn", data={"text": "test", "domain_scope": "munim"})
                assert r_agent.status_code == 200
                assert r_agent.json()["is_error"] is True

                r_people = await client.post(
                    "/api/munim/people",
                    json={"full_name": "Karan Mistri", "person_type": "salaried_karigar"},
                )
                assert r_people.status_code == 200

        # Shreyansh (can_login_munim=True) allowed on /munim
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            auth_type="supabase", identifier="shreyansh@example.com", display_name="Shreyansh"
        )
        with patch("app.routers.domains.get_supabase_admin", return_value=sb):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r_m = await client.get("/api/munim/overview")
                assert r_m.status_code == 200
                assert r_m.json()["me"]["can_login_munim"] is True
                r_sync = await client.post("/api/munim/sync-zoho")
                assert r_sync.status_code == 200
                assert "project_caches" in r_sync.json()
    finally:
        app.dependency_overrides.clear()
