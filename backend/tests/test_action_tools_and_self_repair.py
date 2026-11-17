"""Tests for medium-thinking escalation, 10-goal tray, focused action tools, and silent self-repair."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.services import agent_core
from app.services.agent_core import AgentTurnEnvelope
from app.services.goal_primitives import has_complex_turn_cues
from conftest import FakeSupabase, seed_goal_intelligence_sb


def test_complex_turn_cues_escalate_to_medium_thinking_and_10_open_goals():
    # Routine 1-liners & None stay on fast 'low' thinking
    assert has_complex_turn_cues(None) is False
    assert has_complex_turn_cues("500 chai ansh") is False
    assert has_complex_turn_cues("New lead Arjun 3 rooms") is False

    # Multi-item lists, PDFs, crowded trays & pushback escalate to 'medium'
    assert has_complex_turn_cues("Mom Room (1): 10ft Bed + 7ft Almirah; Room 2: 9'6\" Bed") is True
    assert has_complex_turn_cues("add arjun project", has_pdf=True) is True
    assert has_complex_turn_cues("All these are furniture names only. What are you confusing with?") is True
    assert has_complex_turn_cues("Check status", open_goals_count=5) is True

    # Open goals tray now surfaces up to 10 active goals instead of truncating at 4
    sb = seed_goal_intelligence_sb()
    for i in range(9):
        sb.store["agent_goals"].append(
            {
                "id": f"goal-{i + 1}",
                "owner_person_id": "p-shreyansh",
                "domain_scope": "production",
                "goal_type": "stage_update",
                "status": "gathering_info",
                "summary_title": f"Add Item {i + 1} to Arjun (Jaipur)",
                "missing_slots": ["item_name"],
                "pending_prompt": "Item name?",
                "collected_slots": {"operation_kind": "item_upsert_or_move"},
            }
        )
    ctx_json, _ = agent_core._build_compact_grounding(
        sb, owner_person_id="p-shreyansh", domain_scope="production", ui_context={}
    )
    tray = json.loads(ctx_json)["open_goals_tray"]
    assert len(tray) == 9


@pytest.mark.asyncio
async def test_action_tools_upsert_order_items_and_in_loop_validation_feedback():
    # Case A: Gemini stages 3 furniture items via focused `upsert_order_items` tool -> auto-commits all 3
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {"id": "acc-axis", "code": "axis", "display_name": "Axis Bank", "account_kind": "bank", "aliases": ["axis"], "is_active": True, "sort_order": 1}
            ],
            "people": [
                {"id": "p-shreyansh", "full_name": "Shreyansh", "person_type": "operator", "role_tags": ["owner"], "aliases": [], "payout_aliases": [], "is_active": True}
            ],
            "customers": [{"id": "c-arjun", "full_name": "Arjun", "area_landmark": "Jaipur", "is_active": True}],
            "projects": [
                {
                    "id": "prj-arjun",
                    "project_code": "PRJ-ARJ-1",
                    "customer_id": "c-arjun",
                    "customer_name": "Arjun (Jaipur)",
                    "project_type": "customer",
                    "status": "active",
                    "pipeline_stage": "execution",
                    "material_stage": "check_stock",
                    "payment_stage": "advance_pending",
                }
            ],
            "project_items": [],
            "stage_events": [],
            "agent_goals": [],
            "workshop_memory": [],
            "vendors": [],
        }
    )

    turn1 = SimpleNamespace(
        id="int_items_1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call_up_1",
                name="upsert_order_items",
                arguments={
                    "project_id": "prj-arjun",
                    "items": [
                        {"item_name": "Mom Room (1) - 10ft Bed", "dimensions": "Back 10ft", "to_stage": "in_shop"},
                        {"item_name": "Room 2 - 32\"x93\" Almirah", "dimensions": "32\"x93\""},
                        {"item_name": "6ftx3ft Drawer Unit", "dimensions": "6ftx3ft"},
                    ],
                },
            )
        ],
        output_text=None,
    )
    turn2 = SimpleNamespace(
        id="int_items_2",
        steps=[],
        output_text=AgentTurnEnvelope(
            intent_category="goal_actions",
            direct_reply_text="3 furniture items added to Arjun (Jaipur).",
            goal_items=[],
        ).model_dump_json(),
    )

    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [turn1, turn2]

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
            domain_scope="production",
            text="Mom Room (1): 10ft Bed; Room 2: 32\"x93\" Almirah; 6ftx3ft Drawer Unit",
        )

    assert fake_client.interactions.create.call_args_list[0].kwargs["generation_config"] == {"thinking_level": "medium"}
    assert len(out["goals"]) == 3
    assert all(g["status"] == "completed" for g in out["goals"])
    assert len(sb.store["project_items"]) == 3

    # Case B: Financial tool missing payment_account_code -> in-loop rejection -> Gemini fixes -> awaiting_confirmation!
    fin_turn1 = SimpleNamespace(
        id="int_fin_1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call_fin_1",
                name="stage_financial_entry",
                arguments={"entry_kind": "expense", "amount_inr": 1200, "party_name": "Hafele Hinges"},
            )
        ],
        output_text=None,
    )
    fin_turn2 = SimpleNamespace(
        id="int_fin_2",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call_fin_2",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "expense",
                    "amount_inr": 1200,
                    "party_name": "Hafele Hinges",
                    "payment_account_code": "axis",
                    "expense_category": "raw_materials",
                },
            )
        ],
        output_text=None,
    )
    fin_turn3 = SimpleNamespace(
        id="int_fin_3",
        steps=[],
        output_text=AgentTurnEnvelope(intent_category="goal_actions", goal_items=[]).model_dump_json(),
    )

    fake_fin_client = MagicMock()
    fake_fin_client.interactions.create.side_effect = [fin_turn1, fin_turn2, fin_turn3]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_fin_client),
    ):
        fin_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            text="Hafele hinges 1200 pay kiya",
        )

    assert len(fin_out["goals"]) == 1
    assert fin_out["goals"][0]["status"] == "awaiting_confirmation"
    assert "AXIS" in fin_out["goals"][0]["prompt"]
    first_tool_resp = json.loads(
        fake_fin_client.interactions.create.call_args_list[1].kwargs["input"][0]["result"][0]["text"]
    )
    assert first_tool_resp["staged"] is False
    assert first_tool_resp["missing_slots"][0]["missing_slots"] == ["payment_account_code"]


@pytest.mark.asyncio
async def test_silent_internal_self_repair_fixes_sloppy_envelope_before_prompting_user():
    sb = seed_goal_intelligence_sb()

    # Round 1: Gemini omits area_landmark despite it being clearly stated in a >40-char user message
    sloppy_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="new_lead",
                summary_title="New Project: Vikas Jain",
                full_name="Vikas Jain",
                area_landmark=None,
                rough_scope="2 bedrooms modular wardrobes",
            )
        ],
    ).model_dump_json()

    repaired_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="new_lead",
                summary_title="New Project: Vikas Jain (Civil Lines Jaipur)",
                full_name="Vikas Jain",
                area_landmark="Civil Lines Jaipur",
                rough_scope="2 bedrooms modular wardrobes",
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [
        SimpleNamespace(id="int_sloppy", steps=[], output_text=sloppy_env),
        SimpleNamespace(id="int_repaired", steps=[], output_text=repaired_env),
    ]

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
            domain_scope="production",
            text="Create new order for Vikas Jain in Civil Lines Jaipur for 2 bedrooms modular wardrobes",
        )

    assert fake_client.interactions.create.call_count == 2
    repair_kwargs = fake_client.interactions.create.call_args_list[1].kwargs
    assert repair_kwargs["previous_interaction_id"] == "int_sloppy"
    assert repair_kwargs["generation_config"] == {"thinking_level": "medium"}
    assert out["goals"][0]["status"] == "awaiting_confirmation"
    assert out["goals"][0]["collected_slots"]["area_landmark"] == "Civil Lines Jaipur"


@pytest.mark.asyncio
async def test_financial_dedup_collapses_staged_party_name_and_envelope_person_id():
    sb = seed_goal_intelligence_sb()
    sb.store["people"].append(
        {
            "id": "p-rajesh",
            "full_name": "Rajesh Ji",
            "person_type": "salaried_karigar",
            "role_tags": ["karigar"],
            "is_active": True,
        }
    )

    turn1 = SimpleNamespace(
        id="int_raj_1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call_raj_1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "expense",
                    "amount_inr": 1000,
                    "party_name": "Rajesh Ji",
                    "payment_account_code": "cash_galla",
                    "expense_category": "salary_wages",
                    "notes": "Salary advance",
                },
            )
        ],
        output_text=None,
    )
    turn2 = SimpleNamespace(
        id="int_raj_2",
        steps=[],
        output_text=AgentTurnEnvelope(
            intent_category="goal_actions",
            goal_items=[
                agent_core.GoalIntentItem(
                    operation_kind="expense",
                    summary_title="₹1,000 salary advance to Rajesh Ji",
                    amount_inr=1000,
                    person_id="p-rajesh",
                    payment_account_code="cash_galla",
                    expense_category="salary_wages",
                )
            ],
        ).model_dump_json(),
    )

    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [turn1, turn2]

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
            text="1000 paid to rajesh in cash salary advance",
        )

    assert len(out["goals"]) == 1
    assert out["goals"][0]["collected_slots"]["party_name"] == "Rajesh Ji"
    assert "Rajesh Ji" in out["goals"][0]["prompt"]


@pytest.mark.asyncio
async def test_upgrade_a_draft_id_and_duplicate_financial_guard():
    sb = seed_goal_intelligence_sb()
    sb.store["people"].extend(
        [
            {"id": "p-raj", "full_name": "Rajesh Ji", "person_type": "salaried_karigar", "is_active": True},
            {"id": "p-brij", "full_name": "Brijesh Ji", "person_type": "salaried_karigar", "is_active": True},
        ]
    )

    # 1. Duplicate ₹5,000 Cash Galla entry blocked in-loop; draft_id='D1' updates in-place cleanly
    dup_t1 = SimpleNamespace(
        id="int_d1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="c1",
                name="stage_financial_entry",
                arguments={"entry_kind": "vendor_payment", "amount_inr": 5000, "party_name": "Suresh Ji", "payment_account_code": "cash_galla"},
            ),
            SimpleNamespace(
                type="function_call",
                id="c2",
                name="stage_financial_entry",
                arguments={"entry_kind": "vendor_payment", "amount_inr": 5000, "payment_account_code": "cash_galla"},
            ),
            SimpleNamespace(
                type="function_call",
                id="c3",
                name="stage_financial_entry",
                arguments={"draft_id": "D1", "entry_kind": "vendor_payment", "amount_inr": 5000, "party_name": "Suresh Ji", "payment_account_code": "cash_galla", "notes": "Updated note"},
            ),
        ],
        output_text=None,
    )
    dup_t2 = SimpleNamespace(id="int_d2", steps=[], output_text="")
    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [dup_t1, dup_t2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="5000 cash paid to Suresh Ji",
        )

    assert len(out["goals"]) == 1
    assert out["goals"][0]["collected_slots"]["notes"] == "Updated note"
    fn_results = fake_client.interactions.create.call_args_list[1].kwargs["input"]
    assert json.loads(fn_results[0]["result"][0]["text"])["draft_id"] == "D1"
    assert json.loads(fn_results[1]["result"][0]["text"])["duplicate_of"] == "D1"

    # 2. Distinct payees ("1000 each to Rajesh and Brijesh") pass through as D1 and D2
    split_t1 = SimpleNamespace(
        id="int_s1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="cs1",
                name="stage_financial_entry",
                arguments={"entry_kind": "expense", "amount_inr": 1000, "party_name": "Rajesh Ji", "payment_account_code": "cash_galla", "expense_category": "salary_wages"},
            ),
            SimpleNamespace(
                type="function_call",
                id="cs2",
                name="stage_financial_entry",
                arguments={"entry_kind": "expense", "amount_inr": 1000, "party_name": "Brijesh Ji", "payment_account_code": "cash_galla", "expense_category": "salary_wages"},
            ),
        ],
        output_text=None,
    )
    split_t2 = SimpleNamespace(id="int_s2", steps=[], output_text="")
    fake_split = MagicMock()
    fake_split.interactions.create.side_effect = [split_t1, split_t2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_split),
    ):
        split_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="1000 each paid to Rajesh and Brijesh in cash salary advance",
        )

    assert len(split_out["goals"]) == 2
    assert {g["collected_slots"]["party_name"] for g in split_out["goals"]} == {"Rajesh Ji", "Brijesh Ji"}


@pytest.mark.asyncio
async def test_upgrade_b_payee_matching_homonym_choices_and_cross_table_uuid_guard():
    sb = seed_goal_intelligence_sb()
    sb.store["vendors"] = [
        {"id": "v-suresh", "canonical_name": "Suresh Ji", "vendor_kind": "contractor", "specialty": "Painter", "zoho_contact_id": "zv-suresh", "aliases": [], "is_active": True}
    ]
    # 1. "suresh ji painter" matches Suresh Ji inside tool -> ✅ Matched badge & zoho_contact_id locked
    t1 = SimpleNamespace(
        id="int_b1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="cb1",
                name="stage_financial_entry",
                arguments={"entry_kind": "vendor_payment", "amount_inr": 5000, "party_name": "suresh ji painter", "payment_account_code": "cash_galla"},
            )
        ],
        output_text=None,
    )
    t2 = SimpleNamespace(id="int_b2", steps=[], output_text="")
    fake_client = MagicMock()
    fake_client.interactions.create.side_effect = [t1, t2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="5000 cash paid to suresh ji painter",
        )

    assert len(out["goals"]) == 1
    slots = out["goals"][0]["collected_slots"]
    assert slots["party_name"] == "Suresh Ji"
    assert slots["zoho_contact_id"] == "zv-suresh"
    assert "✅ Matched" in out["goals"][0]["prompt"]

    # 2. Homonym vendors trigger Python-built choices; bogus project UUID in vendor_id is stripped
    pid_uuid = "60e1f4d2-b5c4-4fa7-a8c5-1a8583c6f8f9"
    sb.store["projects"].append({"id": pid_uuid, "project_code": "PRJ-ARJUN", "customer_name": "Arjun", "status": "active"})
    bogus_slots = {"operation_kind": "item_material_upsert", "project_id": pid_uuid, "vendor_id": pid_uuid}
    agent_core._sanitize_and_resolve_slot_ids(sb, bogus_slots)
    assert "vendor_id" not in bogus_slots

    sb.store["vendors"] = [
        {"id": "v-sp", "canonical_name": "Suresh Painter", "vendor_kind": "contractor", "specialty": "Painter", "aliases": [], "is_active": True},
        {"id": "v-sc", "canonical_name": "Suresh Carpenter", "vendor_kind": "contractor", "specialty": "Carpenter", "aliases": [], "is_active": True},
    ]
    amb_t1 = SimpleNamespace(
        id="int_amb1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="ca1",
                name="stage_financial_entry",
                arguments={"entry_kind": "vendor_payment", "amount_inr": 2000, "party_name": "Suresh", "payment_account_code": "cash_galla"},
            )
        ],
        output_text=None,
    )
    amb_t2 = SimpleNamespace(id="int_amb2", steps=[], output_text="")
    fake_amb = MagicMock()
    fake_amb.interactions.create.side_effect = [amb_t1, amb_t2]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_amb),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        amb_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="2000 cash paid to Suresh",
        )

    assert amb_out["goals"][0]["status"] == "gathering_info"
    assert len(amb_out["goals"][0]["choices"]) == 3


@pytest.mark.asyncio
async def test_upgrade_c_no_response_format_plain_text_qa_and_inline_needs_info_reply():
    import httpx
    from app.auth import CurrentUser, get_current_user
    from app.main import app

    sb = seed_goal_intelligence_sb()
    sb.store["vendors"] = [
        {"id": "v-suresh", "canonical_name": "Suresh Ji", "vendor_kind": "contractor", "specialty": "Painter", "zoho_contact_id": "zv-suresh", "aliases": [], "is_active": True}
    ]
    sb.store["agent_goals"].append(
        {
            "id": "goal-ask-vendor",
            "owner_person_id": "p-shreyansh",
            "domain_scope": "munim",
            "goal_type": "financial_txn",
            "status": "gathering_info",
            "summary_title": "₹5,000 paid from Cash Galla",
            "missing_slots": ["party_name"],
            "pending_prompt": "Kripya yeh details batayein (vendor payment): *Customer / Vendor Name*",
            "collected_slots": {
                "operation_kind": "vendor_payment",
                "amount_inr": 5000.0,
                "payment_account_code": "cash_galla",
                "payment_account_id": "acc-galla",
            },
            "steps": [],
            "repair_options": [],
        }
    )

    # 1. Interactions call omits response_format; cart ignores trailing noisy JSON echo; audio HEARD extracted
    t_act = SimpleNamespace(
        id="int_c1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id="cc1",
                name="stage_financial_entry",
                arguments={
                    "entry_kind": "expense",
                    "amount_inr": 300,
                    "party_name": "chai",
                    "payment_account_code": "cash_galla",
                    "expense_category": "food_tea_mess",
                    "heard_transcript": "300 rupaye chai nasta cash se",
                },
            )
        ],
        output_text=None,
    )
    t_echo = SimpleNamespace(
        id="int_c2",
        steps=[],
        output_text='{"intent_category":"goal_actions","goal_items":[{"operation_kind":"expense","summary_title":"duplicate echo"}]}',
    )
    # 2. Native plain-text Q&A (no JSON response_format)
    t_qa = SimpleNamespace(id="int_qa1", steps=[], output_text="HEARD: Cash balance kya hai?\nCash Galla balance ₹10,000 hai.")

    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t_act, t_echo, t_qa]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
    ):
        act_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            audio_bytes=b"ogg-bytes",
        )
        qa_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            audio_bytes=b"ogg-bytes",
            text=None,
        )

    for call in fake_c.interactions.create.call_args_list:
        assert "response_format" not in call.kwargs
    assert len(act_out["goals"]) == 1  # Noisy JSON echo ignored completely!
    assert act_out["heard_transcript"] == "300 rupaye chai nasta cash se"
    assert qa_out["kind"] == "direct_answer"
    assert qa_out["heard_transcript"] == "Cash balance kya hai?"
    assert "Cash Galla balance" in qa_out["reply_text"]

    # 3. Fix #1: POST /api/agent/goals/{goal_id}/reply resolves Suresh Ji Painter -> awaiting_confirmation
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
                r = await client.post(
                    "/api/agent/goals/goal-ask-vendor/reply",
                    json={"text": "Suresh Ji Painter"},
                )
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200
    g_res = r.json()["goals"][0]
    assert g_res["status"] == "awaiting_confirmation"
    assert g_res["collected_slots"]["party_name"] == "Suresh Ji"
    assert g_res["collected_slots"]["zoho_contact_id"] == "zv-suresh"
    assert "✅ Matched" in g_res["prompt"]
