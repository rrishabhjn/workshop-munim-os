from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.routers import telegram as tg_router
from app.services import agent_core, decisions_triage, step_executors, zoho
from app.services.agent_core import AgentTurnEnvelope, ErrorRepairProposal, _inline_json_schema


class FakeTable:
    """In-memory Supabase table mock supporting select/insert/update/delete/eq/in_/ilike."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def _clone_query(self) -> "FakeQuery":
        return FakeQuery(self.rows)

    def select(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
        return self._clone_query().select()

    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> "FakeQuery":
        return self._clone_query().insert(payload)

    def update(self, patch_obj: dict[str, Any]) -> "FakeQuery":
        return self._clone_query().update(patch_obj)

    def delete(self) -> "FakeQuery":
        return self._clone_query().delete()


class FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._op = "select"
        self._payload: Any = None
        self._filters: list[Any] = []
        self._limit: int | None = None

    def select(self) -> "FakeQuery":
        self._op = "select"
        return self

    def insert(self, payload: Any) -> "FakeQuery":
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, patch_obj: dict[str, Any]) -> "FakeQuery":
        self._op = "update"
        self._payload = patch_obj
        return self

    def delete(self) -> "FakeQuery":
        self._op = "delete"
        return self

    def eq(self, col: str, val: Any) -> "FakeQuery":
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col: str, vals: list[Any]) -> "FakeQuery":
        s = set(vals)
        self._filters.append(lambda r: r.get(col) in s)
        return self

    def ilike(self, col: str, pat: str) -> "FakeQuery":
        needle = pat.strip("%").lower()
        self._filters.append(lambda r: needle in str(r.get(col) or "").lower())
        return self

    @property
    def not_(self) -> "FakeQuery":
        return self

    def is_(self, col: str, val: Any) -> "FakeQuery":
        if val == "null":
            self._filters.append(lambda r: r.get(col) is not None)
        return self

    def order(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._limit = n
        return self

    def execute(self) -> SimpleNamespace:
        matched = [r for r in self._rows if all(fn(r) for fn in self._filters)]
        if self._limit is not None and self._op == "select":
            matched = matched[: self._limit]

        if self._op == "select":
            return SimpleNamespace(data=[dict(r) for r in matched])

        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            created = []
            for idx, it in enumerate(items):
                row = {"id": it.get("id") or f"id-{len(self._rows) + idx + 1}", **it}
                self._rows.append(row)
                created.append(dict(row))
            return SimpleNamespace(data=created)

        if self._op == "update":
            updated = []
            for r in matched:
                r.update(self._payload)
                updated.append(dict(r))
            return SimpleNamespace(data=updated)

        if self._op == "delete":
            for r in matched:
                self._rows.remove(r)
            return SimpleNamespace(data=matched)

        return SimpleNamespace(data=[])


class FakeSupabase:
    def __init__(self, initial: dict[str, list[dict[str, Any]]] | None = None):
        self.store: dict[str, list[dict[str, Any]]] = initial or {}

    def table(self, name: str) -> FakeTable:
        return FakeTable(self.store.setdefault(name, []))


def _seed_fake_sb() -> FakeSupabase:
    return FakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-cash",
                    "code": "cash_galla",
                    "display_name": "Cash Galla",
                    "account_kind": "cash_galla",
                    "aliases": ["cash"],
                    "is_active": True,
                    "sort_order": 1,
                },
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis"],
                    "is_active": True,
                    "sort_order": 2,
                },
            ],
            "people": [
                {
                    "id": "p-shreyansh",
                    "full_name": "Shreyansh",
                    "person_type": "operator",
                    "role_tags": ["owner", "qc"],
                    "aliases": [],
                    "payout_aliases": [],
                    "is_default_purchase": False,
                    "is_default_qc": True,
                    "telegram_user_id": 1001,
                    "is_active": True,
                },
                {
                    "id": "p-ansh",
                    "full_name": "Ansh",
                    "person_type": "operator",
                    "role_tags": ["purchase"],
                    "aliases": [],
                    "payout_aliases": [],
                    "is_default_purchase": True,
                    "is_default_qc": False,
                    "telegram_user_id": 1002,
                    "is_active": True,
                },
                {
                    "id": "p-ramesh",
                    "full_name": "Ramesh Mistri",
                    "person_type": "salaried_karigar",
                    "role_tags": ["mistri"],
                    "aliases": ["Ramu"],
                    "payout_aliases": [],
                    "is_default_purchase": False,
                    "is_default_qc": False,
                    "is_active": True,
                },
            ],
            "customers": [
                {
                    "id": "c-ambure",
                    "full_name": "Ambure Ji",
                    "area_landmark": "Jaipur",
                    "phone": "9829000001",
                    "aliases": [],
                    "is_active": True,
                }
            ],
            "projects": [
                {
                    "id": "prj-ambure",
                    "project_code": "PRJ-41",
                    "customer_id": "c-ambure",
                    "customer_name": "Ambure Ji (Jaipur)",
                    "project_type": "customer",
                    "status": "active",
                    "pipeline_stage": "site_sizing_boq",
                    "material_stage": "not_started",
                    "payment_stage": "advance_in",
                    "quoted_total": 2300000.0,
                    "paid_cache_inr": 300000.0,
                    "boq_scope_changed": False,
                }
            ],
            "project_items": [],
            "stage_events": [],
            "agent_goals": [],
            "workshop_memory": [],
            "vendors": [],
        }
    )


def test_inline_json_schema_has_no_defs_or_refs():
    for cls in (AgentTurnEnvelope, ErrorRepairProposal):
        schema = _inline_json_schema(cls)
        dumped = json.dumps(schema)
        assert '"$defs"' not in dumped
        assert '"$ref"' not in dumped


def test_decisions_triage_media_rubric_escalates_thinking():
    fake_resp = SimpleNamespace(
        answers={
            "media_kind": SimpleNamespace(choice="supplier_bill", confidence=0.94),
            "ocr_difficulty": SimpleNamespace(score=1.82),
        }
    )
    fake_client = MagicMock()
    fake_client.decisions.create.return_value = fake_resp

    with (
        patch("app.services.decisions_triage.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.decisions_triage._get_decisions_client", return_value=fake_client),
    ):
        res = decisions_triage.triage_media_upload(b"fake-jpg", "image/jpeg", "Patel ply parchi")

    assert res.media_kind == "supplier_bill"
    assert res.recommended_thinking == "high"


@pytest.mark.asyncio
async def test_agent_core_tool_loop_auto_commit_and_one_tap_undo():
    sb = _seed_fake_sb()

    # Round 1: Gemini calls search_projects_and_items
    turn1 = SimpleNamespace(
        id="int_turn_1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call_1",
                name="search_projects_and_items",
                arguments={"query": "Ambure"},
            )
        ],
        output_text=None,
    )
    # Round 2: Gemini returns unambiguous pipeline_stage_move -> execution
    envelope_json = AgentTurnEnvelope(
        heard_transcript="Ambure ji ka kaam execution mein daal do",
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="pipeline_stage_move",
                summary_title="Move Ambure Ji to Execution",
                project_id="prj-ambure",
                to_stage="execution",
            )
        ],
    ).model_dump_json()
    turn2 = SimpleNamespace(id="int_turn_2", steps=[], output_text=envelope_json)

    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [turn1, turn2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
        patch("app.services.telegram_api.send_message", new_callable=AsyncMock) as mock_ping,
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            audio_bytes=b"fake-ogg-bytes",
        )

    # Verify Round 2 used previous_interaction_id within-turn and explicit thinking_level
    assert fake_client.interactions.create.call_count == 2
    second_call = fake_client.interactions.create.call_args_list[1].kwargs
    assert second_call["previous_interaction_id"] == "int_turn_1"
    assert second_call["generation_config"] == {"thinking_level": "low"}

    # Auto-committed immediately + Ansh auto-assigned to check_stock + pinged!
    assert out["heard_transcript"] == "Ambure ji ka kaam execution mein daal do"
    goal_res = out["goals"][0]
    assert goal_res["status"] == "completed"
    assert goal_res["undo_available"] is True
    assert sb.store["projects"][0]["pipeline_stage"] == "execution"
    assert sb.store["projects"][0]["material_stage"] == "check_stock"
    assert sb.store["projects"][0]["material_owner_id"] == "p-ansh"
    mock_ping.assert_awaited_once()  # Anti-spam handoff ping sent to Ansh!

    # Now test 1-tap [↩️ Undo] -> restores both pipeline_stage and material_stage!
    undo_res = agent_core.undo_completed_goal(sb, goal_id=goal_res["goal_id"], actor_person_id="p-shreyansh")
    assert undo_res["ok"] is True
    assert sb.store["projects"][0]["pipeline_stage"] == "site_sizing_boq"
    assert sb.store["projects"][0]["material_stage"] == "not_started"
    assert sb.store["agent_goals"][0]["status"] == "undone"


@pytest.mark.asyncio
async def test_compound_reply_resolves_open_goal_and_creates_financial_confirm_card():
    sb = _seed_fake_sb()
    # Pre-seed an open goal waiting for Manoj Sharma's area_landmark
    sb.store["agent_goals"].append(
        {
            "id": "goal-open-1",
            "owner_person_id": "p-shreyansh",
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New Lead: Manoj Sharma",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Share Site Area for Manoj Sharma",
            "collected_slots": {
                "operation_kind": "new_lead",
                "full_name": "Manoj Sharma",
                "rough_scope": "Modular Kitchen",
            },
            "steps": [],
        }
    )

    # Single compound sentence answers goal-open-1 AND logs ₹5,00,000 receipt from Ambure Ji!
    envelope_json = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                target_open_goal_id="goal-open-1",
                operation_kind="new_lead",
                summary_title="New Lead: Manoj Sharma (Ajmer Road)",
                area_landmark="Ajmer Road",
            ),
            agent_core.GoalIntentItem(
                operation_kind="customer_receipt",
                summary_title="₹5L Cash from Ambure Ji",
                project_id="prj-ambure",
                party_name="Ambure Ji",
                amount_inr=500000,
                payment_account_code="cash_galla",
            ),
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_compound", steps=[], output_text=envelope_json
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            text="Manoj Sharma Ajmer Road ke hain, aur Ambure ji se 5 lakh cash aaya",
        )

    assert len(out["goals"]) == 2
    assert out["goals"][0]["goal_id"] == "goal-open-1"
    assert out["goals"][0]["status"] == "awaiting_confirmation"
    assert out["goals"][1]["status"] == "awaiting_confirmation"
    assert "₹5,00,000" in out["goals"][1]["prompt"]

    # Confirm the ₹5,00,000 receipt -> Smart Auto-Bridge updates Ambure Ji paid_cache_inr to ₹8,00,000 ('partial_paid')!
    with patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)):
        exec_res = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id=out["goals"][1]["goal_id"],
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )
    assert exec_res["status"] == "completed"
    assert sb.store["projects"][0]["paid_cache_inr"] == 800000.0
    assert sb.store["projects"][0]["payment_stage"] == "partial_paid"


@pytest.mark.asyncio
async def test_zoho_400_error_triggers_hydrogen_self_healing_and_clerk_park():
    sb = _seed_fake_sb()
    sb.store["agent_goals"].append(
        {
            "id": "goal-err-1",
            "owner_person_id": "p-shreyansh",
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Vendor Payment to Patel Ply",
            "collected_slots": {
                "operation_kind": "vendor_payment",
                "party_name": "Patel Ply",
                "amount_inr": 40000,
                "payment_account_id": "acc-axis",
            },
            "steps": [
                {
                    "step_uuid": "s-1",
                    "step_index": 0,
                    "target": "zoho",
                    "operation": "vendor_payment",
                    "status": "pending",
                }
            ],
        }
    )

    repair_json = ErrorRepairProposal(
        explanation_hinglish="Zoho mein 'Patel Ply' customer ke naam se darj hai, isliye vendor payment ruk gaya.",
        repair_options=[
            agent_core.DisambiguationChoice(
                choice_id="rename_vendor",
                label="Save as 'Patel Ply (Vendor)'",
                slot_patch_json='{"party_name": "Patel Ply (Vendor)"}',
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_heal", steps=[], output_text=repair_json
    )

    with (
        patch(
            "app.services.zoho.push_management_entry_to_zoho",
            new=AsyncMock(
                return_value={
                    "ok": False,
                    "status_code": 400,
                    "error_kind": "validation_400",
                    "error": {"code": 3001, "message": "Contact exists as customer"},
                }
            ),
        ),
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        res = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-err-1",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res["status"] == "needs_error_fix"
    assert res["can_park_for_clerk"] is True
    assert len(res["choices"]) == 1
    assert fake_client.interactions.create.call_args.kwargs["generation_config"] == {"thinking_level": "medium"}

    # Shreyansh taps [📥 Let Clerk Fix Later]
    parked = agent_core.park_goal_for_clerk(sb, "goal-err-1")
    assert parked["status"] == "parked_for_clerk"
    assert sb.store["agent_goals"][0]["status"] == "parked_for_clerk"


@pytest.mark.asyncio
async def test_fastapi_domains_and_debounced_telegram_webhook_endpoints():
    import httpx
    from app.auth import CurrentUser, get_current_user
    from app.main import app

    sb = _seed_fake_sb()
    sb.store["project_pipeline_stages"] = [{"code": "lead", "label": "Lead", "sort_order": 1}]
    sb.store["material_track_stages"] = [{"code": "not_started", "label": "Not Started", "sort_order": 1}]
    sb.store["payment_track_stages"] = [{"code": "advance_pending", "label": "Advance Pending", "sort_order": 1}]
    sb.store["workshop_stages"] = [{"code": "in_shop", "label": "In Shop", "sort_order": 1}]
    sb.store["v_whos_doing_what"] = []
    sb.store["v_needs_info_queue"] = []
    sb.store["v_project_pnl"] = []

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="telegram", identifier="1001", display_name="Shreyansh"
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("app.routers.domains.get_supabase_admin", return_value=sb):
                r_prod = await client.get("/api/production/overview")
                assert r_prod.status_code == 200
                assert r_prod.json()["me"]["person_id"] == "p-shreyansh"

                r_munim = await client.get("/api/munim/overview")
                assert r_munim.status_code == 200
                assert len(r_munim.json()["payment_accounts"]) == 2

                r_lead = await client.post(
                    "/api/production/orders",
                    json={
                        "full_name": "Lodha Ji",
                        "area_landmark": "Beawar",
                        "rough_scope": "Full Bedroom",
                    },
                )
                assert r_lead.status_code == 200
                assert r_lead.json()["ok"] is True

            # Test /telegram/webhook secret check + 2.5s burst buffering
            from app.config import get_settings

            app.dependency_overrides[get_settings] = lambda: Settings(
                telegram_webhook_secret="sec-xyz",
                allowed_telegram_user_ids="1001",
                telegram_burst_debounce_seconds=0.0,
            )
            with (
                patch("app.routers.telegram.get_supabase_admin", return_value=sb),
                patch("app.routers.telegram.process_debounced_burst", new=AsyncMock()),
            ):
                bad = await client.post("/telegram/webhook", json={"update_id": 1})
                assert bad.status_code == 403

                ok = await client.post(
                    "/telegram/webhook",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "sec-xyz"},
                    json={
                        "update_id": 9001,
                        "message": {
                            "message_id": 10,
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "chat": {"id": 1001, "type": "private"},
                            "text": "Ambure ji ka kitchen pack ho gaya",
                        },
                    },
                )
                assert ok.status_code == 200
                assert ok.json()["buffered"] is True
                assert len(sb.store["telegram_inbox_buffer"]) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_master_spine_low_risk_auto_commit_cascade_zoho_sync_and_undo():
    sb = _seed_fake_sb()
    sb.store["customers"][0]["zoho_contact_id"] = "zc-ambure-99"

    envelope_json = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="update_customer",
                summary_title="Update Ambure Ji phone & area",
                customer_id="c-ambure",
                phone="9999988888",
                area_landmark="Vaishali Nagar Jaipur",
                aliases=["Ambure Sir"],
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_cust_upd", steps=[], output_text=envelope_json
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
        patch("app.services.zoho.sync_existing_zoho_contact", new=AsyncMock(return_value=True)) as mock_zoho_sync,
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            text="Ambure ji ka phone 9999988888 aur area Vaishali Nagar Jaipur kar do",
        )

    goal_out = out["goals"][0]
    assert goal_out["status"] == "completed"
    assert goal_out["undo_available"] is True
    assert sb.store["customers"][0]["phone"] == "9999988888"
    assert sb.store["customers"][0]["area_landmark"] == "Vaishali Nagar Jaipur"
    assert "Ambure Sir" in sb.store["customers"][0]["aliases"]
    # Cascaded onto active project!
    assert sb.store["projects"][0]["customer_name"] == "Ambure Ji (Vaishali Nagar Jaipur)"
    assert sb.store["projects"][0]["site_area_landmark"] == "Vaishali Nagar Jaipur"
    assert sb.store["projects"][0]["phone"] == "9999988888"
    mock_zoho_sync.assert_awaited_once()

    # 1-Tap [↩️ Undo] restores BOTH customer and active project!
    with patch("app.services.zoho.sync_existing_zoho_contact", new=AsyncMock(return_value=True)):
        undo_res = agent_core.undo_completed_goal(sb, goal_id=goal_out["goal_id"], actor_person_id="p-shreyansh")
    assert undo_res["ok"] is True
    assert sb.store["customers"][0]["phone"] == "9829000001"
    assert sb.store["customers"][0]["area_landmark"] == "Jaipur"
    assert sb.store["projects"][0]["customer_name"] == "Ambure Ji (Jaipur)"


@pytest.mark.asyncio
async def test_master_spine_high_sensitivity_edits_and_archive_require_confirm():
    sb = _seed_fake_sb()

    # 1. Updating Order quoted_total ₹ + adding Worker bank payout_aliases + Soft-Archiving a Person
    envelope_json = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="update_order_details",
                summary_title="Update Ambure Ji deal value to ₹25,00,000",
                project_id="prj-ambure",
                quoted_total=2500000,
            ),
            agent_core.GoalIntentItem(
                operation_kind="update_person",
                summary_title="Add bank proxy Ruksar Bano for Ramesh Mistri",
                person_id="p-ramesh",
                payout_aliases=["Ruksar Bano"],
            ),
            agent_core.GoalIntentItem(
                operation_kind="archive_record",
                summary_title="Archive Ramesh Mistri",
                archive_entity_kind="person",
                person_id="p-ramesh",
            ),
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_sens", steps=[], output_text=envelope_json
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Ambure deal 25L karo, Ramesh bank Ruksar Bano, aur Ramesh archive karo",
        )

    # All 3 MUST pause for confirmation with Before -> After diffs!
    for g in out["goals"]:
        assert g["status"] == "awaiting_confirmation"
    assert "₹23,00,000" in out["goals"][0]["prompt"]
    assert "₹25,00,000" in out["goals"][0]["prompt"]
    assert "Ruksar Bano" in out["goals"][1]["prompt"]
    assert "Soft-Archive" in out["goals"][2]["prompt"]
    assert sb.store["projects"][0]["quoted_total"] == 2300000.0
    assert sb.store["people"][2]["is_active"] is True

    # Confirm all 3 -> executes cleanly without hard-deleting any row!
    for g in out["goals"]:
        res = await agent_core.confirm_and_execute_goal(
            sb, goal_id=g["goal_id"], actor_person_id="p-shreyansh", actor_label="Shreyansh"
        )
        assert res["status"] == "completed"

    assert sb.store["projects"][0]["quoted_total"] == 2500000.0
    assert "Ruksar Bano" in sb.store["people"][2]["payout_aliases"]
    assert sb.store["people"][2]["is_active"] is False
    assert len(sb.store["people"]) == 3  # Soft-archived, NOT SQL deleted!


@pytest.mark.asyncio
async def test_munim_master_directory_patch_endpoints():
    import httpx
    from app.auth import CurrentUser, get_current_user
    from app.main import app

    sb = _seed_fake_sb()
    sb.store["vendors"].append(
        {
            "id": "v-patel",
            "canonical_name": "Patel Ply",
            "vendor_kind": "supplier",
            "phone": None,
            "gstin": None,
            "specialty": "Plywood",
            "aliases": [],
            "notes": None,
            "is_active": True,
        }
    )

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="telegram", identifier="1001", display_name="Shreyansh"
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch("app.routers.domains.get_supabase_admin", return_value=sb),
                patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
            ):
                rc = await client.patch(
                    "/api/munim/customers/c-ambure",
                    json={"phone": "9111222333", "area_landmark": "C-Scheme Jaipur"},
                )
                assert rc.status_code == 200
                assert sb.store["projects"][0]["customer_name"] == "Ambure Ji (C-Scheme Jaipur)"

                rv = await client.patch(
                    "/api/munim/vendors/v-patel",
                    json={"gstin": "08abcde1234f1z5", "phone": "9414001122"},
                )
                assert rv.status_code == 200
                assert sb.store["vendors"][0]["gstin"] == "08ABCDE1234F1Z5"

                rp = await client.patch(
                    "/api/munim/people/p-ramesh",
                    json={"payout_aliases": ["Ruksar Bano"], "aliases": ["Ramu", "Ramesh Ji"]},
                )
                assert rp.status_code == 200
                assert sb.store["people"][2]["payout_aliases"] == ["Ruksar Bano"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_group_plain_reply_jaipur_bypasses_triage_and_advances_open_goal():
    """Exact reproduction: Shreyansh replies 'Jaipur' without swipe-reply after bot asked in msg 23."""
    sb = _seed_fake_sb()
    now_utc = datetime.now(timezone.utc)
    chat_id = -1003981258923
    sb.store["agent_goals"].append(
        {
            "id": "goal-arjun-1",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "production",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 23,
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New Project: Arjun — 3 Rooms",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Kripya yeh details batayein (new lead): *Site Area / Landmark (e.g. Civil Lines, Jaipur)*",
            "collected_slots": {
                "operation_kind": "new_lead",
                "full_name": "Arjun",
                "rough_scope": "3 Rooms (Beds, Almirahs & Drawer Unit)",
            },
            "steps": [],
            "updated_at": (now_utc - timedelta(minutes=44)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=44)).isoformat(),
        }
    )
    sb.store["telegram_inbox_buffer"] = [
        {
            "id": "buf-jaipur-24",
            "telegram_update_id": 785518847,
            "chat_id": chat_id,
            "user_id": 1001,
            "domain_channel": "production",
            "message_id": 24,
            "reply_to_message_id": None,
            "status": "buffered",
            "created_at": (now_utc - timedelta(seconds=5)).isoformat(),
            "raw_payload": {
                "update_id": 785518847,
                "message": {
                    "message_id": 24,
                    "chat": {"id": chat_id, "type": "supergroup", "title": "FF Orders and Floor"},
                    "from": {"id": 1001, "is_bot": False, "first_name": "Shreyansh"},
                    "text": "Jaipur",
                },
            },
        }
    ]

    envelope_json = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                target_open_goal_id="goal-arjun-1",
                operation_kind="new_lead",
                summary_title="New Project: Arjun (Jaipur) — 3 Rooms",
                area_landmark="Jaipur",
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_jaipur", steps=[], output_text=envelope_json
    )
    mock_triage = MagicMock(return_value=False)
    mock_send = AsyncMock(return_value={"ok": True, "result": {"message_id": 25}})

    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.routers.telegram.decisions_triage.is_group_message_relevant", mock_triage),
        patch("app.routers.telegram.telegram_api.send_message", mock_send),
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        await tg_router.process_debounced_burst(chat_id, 1001, wait_locally=False)

    # Item #2 fast-pass skipped Decisions API altogether
    mock_triage.assert_not_called()
    assert sb.store["telegram_inbox_buffer"][0]["status"] == "processed"
    assert sb.store["agent_goals"][0]["status"] == "awaiting_confirmation"
    assert sb.store["agent_goals"][0]["collected_slots"]["area_landmark"] == "Jaipur"
    assert sb.store["agent_goals"][0]["telegram_bot_msg_id"] == 25
    mock_send.assert_awaited_once()
    sent_kwargs = mock_send.await_args.kwargs
    assert "Confirm New Lead" in sent_kwargs["text"]
    assert "Jaipur" in sent_kwargs["text"]
    btn_texts = [b["text"] for row in sent_kwargs["reply_markup"]["inline_keyboard"] for b in row]
    assert "✅ Confirm" in btn_texts


@pytest.mark.asyncio
async def test_context_aware_decisions_triage_and_social_chatter_guard():
    """Item #1: Social chatter stays ignored even right after bot prompt; delayed answers pass pending context."""
    sb = _seed_fake_sb()
    now_utc = datetime.now(timezone.utc)
    chat_id = -1003981258923
    sb.store["agent_goals"].append(
        {
            "id": "goal-arjun-2",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "production",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 23,
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New Project: Arjun — 3 Rooms",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Kripya yeh details batayein: *Site Area / Landmark*",
            "collected_slots": {"operation_kind": "new_lead", "full_name": "Arjun", "rough_scope": "3 Rooms"},
            "updated_at": (now_utc - timedelta(minutes=50)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=50)).isoformat(),
        }
    )

    # Case A: Pure social greeting right after msg 23 (message_id=24) must NOT fast-pass, and gets ignored
    sb.store["telegram_inbox_buffer"] = [
        {
            "id": "buf-gm-24",
            "telegram_update_id": 91001,
            "chat_id": chat_id,
            "user_id": 1001,
            "domain_channel": "production",
            "message_id": 24,
            "reply_to_message_id": None,
            "status": "buffered",
            "created_at": (now_utc - timedelta(seconds=5)).isoformat(),
            "raw_payload": {
                "update_id": 91001,
                "message": {
                    "message_id": 24,
                    "chat": {"id": chat_id, "type": "supergroup"},
                    "from": {"id": 1001, "first_name": "Shreyansh"},
                    "text": "Good morning",
                },
            },
        }
    ]
    fake_dec_client = MagicMock()
    fake_dec_client.decisions.create.return_value = SimpleNamespace(
        answers={"is_workshop_relevant": SimpleNamespace(noul=0.05)}
    )
    mock_agent_run = AsyncMock()
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.services.decisions_triage.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.decisions_triage._get_decisions_client", return_value=fake_dec_client),
        patch("app.routers.telegram.agent_core.run_agent_turn", mock_agent_run),
    ):
        await tg_router.process_debounced_burst(chat_id, 1001, wait_locally=False)

    assert sb.store["telegram_inbox_buffer"][0]["status"] == "ignored"
    mock_agent_run.assert_not_awaited()
    assert fake_dec_client.decisions.create.call_count == 1

    # Case B: Delayed 'Jaipur' (message_id=40, >2 gap and >45m old) hits Decisions API WITH PendingOpenGoals context
    sb.store["telegram_inbox_buffer"] = [
        {
            "id": "buf-jaipur-40",
            "telegram_update_id": 91002,
            "chat_id": chat_id,
            "user_id": 1001,
            "domain_channel": "production",
            "message_id": 40,
            "reply_to_message_id": None,
            "status": "buffered",
            "created_at": (now_utc - timedelta(seconds=5)).isoformat(),
            "raw_payload": {
                "update_id": 91002,
                "message": {
                    "message_id": 40,
                    "chat": {"id": chat_id, "type": "supergroup"},
                    "from": {"id": 1001, "first_name": "Shreyansh"},
                    "text": "Jaipur",
                },
            },
        }
    ]
    fake_dec_client.decisions.create.reset_mock()
    fake_dec_client.decisions.create.return_value = SimpleNamespace(
        answers={"is_workshop_relevant": SimpleNamespace(noul=0.92)}
    )
    mock_agent_run = AsyncMock(return_value={"kind": "direct_reply", "reply_text": "Noted.", "goals": []})
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.services.decisions_triage.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.decisions_triage._get_decisions_client", return_value=fake_dec_client),
        patch("app.routers.telegram.agent_core.run_agent_turn", mock_agent_run),
        patch("app.routers.telegram.telegram_api.send_message", AsyncMock(return_value={"result": {"message_id": 41}})),
    ):
        await tg_router.process_debounced_burst(chat_id, 1001, wait_locally=False)

    assert sb.store["telegram_inbox_buffer"][0]["status"] == "processed"
    mock_agent_run.assert_awaited_once()
    dec_input = fake_dec_client.decisions.create.call_args.kwargs["input"]
    assert "PendingOpenGoals:" in dec_input
    assert "missing=[area_landmark]" in dec_input
    assert "Site Area / Landmark" in dec_input


@pytest.mark.asyncio
async def test_cross_operator_group_reply_pins_bot_msg_and_preserves_owner():
    """Item #3: Ansh swipe-replying to Shreyansh's open prompt pins the goal in tray and preserves owner_person_id."""
    sb = _seed_fake_sb()
    now_utc = datetime.now(timezone.utc)
    chat_id = -1003981258923
    sb.store["agent_goals"].append(
        {
            "id": "goal-shreyansh-open",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "production",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 23,
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New Project: Arjun",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Site Area / Landmark?",
            "collected_slots": {
                "operation_kind": "new_lead",
                "full_name": "Arjun",
                "rough_scope": "3 Rooms",
            },
            "steps": [],
            "updated_at": (now_utc - timedelta(minutes=20)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=20)).isoformat(),
        }
    )

    envelope_json = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                target_open_goal_id="goal-shreyansh-open",
                operation_kind="new_lead",
                summary_title="New Project: Arjun (Jaipur)",
                area_landmark="Jaipur",
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_cross", steps=[], output_text=envelope_json
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-ansh",
            actor_label="Ansh",
            source_channel="telegram",
            domain_scope="production",
            text="Jaipur",
            telegram_chat_id=chat_id,
            reply_to_bot_msg_id=23,
        )

    sys_inst = fake_client.interactions.create.call_args.kwargs["system_instruction"]
    ctx_json = json.loads(sys_inst.split("CONTEXT:\n", 1)[1])
    tray = ctx_json["open_goals_tray"]
    assert len(tray) == 1
    assert tray[0]["goal_id"] == "goal-shreyansh-open"
    assert tray[0]["replied_directly"] is True

    assert out["goals"][0]["status"] == "awaiting_confirmation"
    # Original owner_person_id must stay p-shreyansh even though Ansh supplied the answer!
    assert sb.store["agent_goals"][0]["owner_person_id"] == "p-shreyansh"
    assert sb.store["agent_goals"][0]["collected_slots"]["area_landmark"] == "Jaipur"


@pytest.mark.asyncio
async def test_single_slot_quick_pick_buttons_and_callback_resolution():
    """Item #4: Single-slot gathering_info gets compact 2x2 quick-pick choices & callback patches resume cleanly."""
    sb = _seed_fake_sb()

    # Case A: new_lead missing only area_landmark -> 4 Rajasthan city quick-picks in 2x2 grid
    lead_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="new_lead",
                summary_title="New Project: Arjun",
                full_name="Arjun",
                rough_scope="3 Rooms",
            )
        ],
    ).model_dump_json()
    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_qp_lead", steps=[], output_text=lead_env
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out_lead = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            text="New lead Arjun 3 rooms",
            telegram_chat_id=-1003981258923,
        )

    g_lead = out_lead["goals"][0]
    assert g_lead["status"] == "gathering_info"
    assert [c["label"] for c in g_lead["choices"]] == [
        "📍 Beawar",
        "📍 Jaipur",
        "📍 Ajmer",
        "📍 Chittorgarh",
    ]
    kb = tg_router._build_goal_keyboard(g_lead)
    assert kb is not None
    # 2 rows of 2 city quick-picks + 1 bottom row [❌ Discard]
    assert len(kb["inline_keyboard"]) == 3
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["📍 Beawar", "📍 Jaipur"]
    assert [b["text"] for b in kb["inline_keyboard"][1]] == ["📍 Ajmer", "📍 Chittorgarh"]
    assert [b["text"] for b in kb["inline_keyboard"][2]] == ["❌ Discard"]

    # Tapping [📍 Jaipur] (index 1) via Telegram callback transitions to awaiting_confirmation
    cq = {
        "id": "cq-jaipur-1",
        "from": {"id": 1001, "first_name": "Shreyansh"},
        "message": {"message_id": 23, "chat": {"id": -1003981258923}},
        "data": f"ag_choice:{g_lead['goal_id']}:1",
    }
    mock_edit = AsyncMock(return_value={"ok": True})
    with (
        patch("app.routers.telegram.telegram_api.answer_callback_query", AsyncMock()),
        patch("app.routers.telegram.telegram_api.edit_message_text", mock_edit),
    ):
        await tg_router._handle_agent_callback(sb, cq)

    assert sb.store["agent_goals"][0]["status"] == "awaiting_confirmation"
    assert sb.store["agent_goals"][0]["collected_slots"]["area_landmark"] == "Jaipur"
    assert "Confirm New Lead" in mock_edit.await_args.kwargs["text"]

    # Case B: customer_receipt missing only payment_account_code -> account quick-picks + resolves payment_account_id
    rcpt_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="customer_receipt",
                summary_title="₹50,000 from Ambure Ji",
                project_id="prj-ambure",
                party_name="Ambure Ji",
                amount_inr=50000,
            )
        ],
    ).model_dump_json()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_qp_rcpt", steps=[], output_text=rcpt_env
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out_rcpt = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Ambure ji se 50000 aaye",
            telegram_chat_id=-1004481937487,
        )

    g_rcpt = out_rcpt["goals"][0]
    assert g_rcpt["status"] == "gathering_info"
    assert [c["label"] for c in g_rcpt["choices"]] == ["💵 Cash Galla", "🏦 Axis Bank"]
    patch_res = await agent_core.apply_slot_patch_and_resume(
        sb,
        goal_id=g_rcpt["goal_id"],
        slot_patch={"payment_account_code": "cash_galla"},
        actor_person_id="p-shreyansh",
        actor_label="Shreyansh",
        source_channel="telegram",
    )
    assert patch_res["status"] == "awaiting_confirmation"
    assert patch_res["collected_slots"]["payment_account_id"] == "acc-cash"
    assert "CASH_GALLA" in patch_res["prompt"]


@pytest.mark.asyncio
async def test_open_goal_area_landmark_backfill_and_swipe_reply_binding():
    """Verify schema descriptions and deterministic area_landmark / swipe-reply backfill."""
    # 1. Schema descriptions exist on full_name, area_landmark, and rough_scope
    schema = _inline_json_schema(AgentTurnEnvelope)
    item_props = schema["properties"]["goal_items"]["items"]["properties"]
    for field_name in ("full_name", "area_landmark", "rough_scope"):
        desc = str(item_props[field_name].get("description") or "").strip()
        assert desc, f"Expected non-empty description for {field_name}"

    # 2. Reproduction of message_id 31 (f213177d): user sends 'Jaipur' for missing_slots=['area_landmark']
    sb1 = _seed_fake_sb()
    now_utc = datetime.now(timezone.utc)
    chat_id = -1003981258923
    sb1.store["agent_goals"].append(
        {
            "id": "goal-msg31",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "production",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 30,
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New order for Arjun — 3 Rooms Beds, Almirahs & Drawer Unit",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Kripya yeh details batayein (new lead): *Site Area / Landmark (e.g. Civil Lines, Jaipur)*",
            "collected_slots": {
                "operation_kind": "new_lead",
                "full_name": "Arjun",
                "rough_scope": "3 Rooms Beds, Almirahs & Drawer Unit",
                "to_stage": "execution",
            },
            "steps": [],
            "updated_at": (now_utc - timedelta(minutes=2)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=2)).isoformat(),
        }
    )

    env_msg31 = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                target_open_goal_id="goal-msg31",
                operation_kind="new_lead",
                summary_title="New order for Arjun (Jaipur) — 3 Rooms",
                area_landmark=None,
            )
        ],
    ).model_dump_json()

    fake_client_1 = MagicMock()
    fake_client_1.interactions.create.return_value = SimpleNamespace(
        id="int_msg31", steps=[], output_text=env_msg31
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client_1),
    ):
        out1 = await agent_core.run_agent_turn(
            sb1,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            text="Jaipur",
            telegram_chat_id=chat_id,
            reply_to_bot_msg_id=30,
        )

    assert len(sb1.store["agent_goals"]) == 1
    assert out1["goals"][0]["goal_id"] == "goal-msg31"
    assert out1["goals"][0]["status"] == "awaiting_confirmation"
    assert out1["goals"][0]["collected_slots"]["area_landmark"] == "Jaipur"

    # 3. Reproduction of message_id 27 (2294f7b0): swipe-reply omits BOTH target_open_goal_id and area_landmark
    sb2 = _seed_fake_sb()
    sb2.store["agent_goals"].append(
        {
            "id": "goal-msg27",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "production",
            "telegram_chat_id": chat_id,
            "telegram_bot_msg_id": 23,
            "goal_type": "new_lead",
            "status": "gathering_info",
            "summary_title": "New Project: Arjun — 3 Rooms",
            "missing_slots": ["area_landmark"],
            "pending_prompt": "Kripya yeh details batayein (new lead): *Site Area / Landmark*",
            "collected_slots": {
                "operation_kind": "new_lead",
                "full_name": "Arjun",
                "rough_scope": "Mom Room Bed, Room 2 Bed & Almirahs",
            },
            "steps": [],
            "updated_at": (now_utc - timedelta(minutes=10)).isoformat(),
            "created_at": (now_utc - timedelta(minutes=10)).isoformat(),
        }
    )

    env_msg27 = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                target_open_goal_id=None,
                operation_kind="new_lead",
                summary_title="New Lead: Arjun (Jaipur)",
                full_name="Arjun",
                area_landmark=None,
            )
        ],
    ).model_dump_json()

    fake_client_2 = MagicMock()
    fake_client_2.interactions.create.return_value = SimpleNamespace(
        id="int_msg27", steps=[], output_text=env_msg27
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client_2),
    ):
        out2 = await agent_core.run_agent_turn(
            sb2,
            owner_person_id="p-ansh",
            actor_label="Ansh",
            source_channel="telegram",
            domain_scope="production",
            text="Nahi, the new project Arjun is for Site Area Jaipur",
            telegram_chat_id=chat_id,
            reply_to_bot_msg_id=23,
        )

    assert len(sb2.store["agent_goals"]) == 1
    assert out2["goals"][0]["goal_id"] == "goal-msg27"
    assert out2["goals"][0]["status"] == "awaiting_confirmation"
    assert out2["goals"][0]["collected_slots"]["area_landmark"] == "Jaipur"
    assert out2["goals"][0]["collected_slots"]["rough_scope"] == "Mom Room Bed, Room 2 Bed & Almirahs"
    assert sb2.store["agent_goals"][0]["owner_person_id"] == "p-shreyansh"


