import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
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

