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
