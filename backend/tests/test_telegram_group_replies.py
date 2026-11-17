"""Tests for Telegram group plain-text/swipe replies, Decisions triage, cross-operator tray pinning, and quick-picks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.routers import telegram as tg_router
from app.services import agent_core
from app.services.agent_core import AgentTurnEnvelope, _inline_json_schema
from conftest import seed_phase2_sb as _seed_fake_sb


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
    assert fake_dec_client.decisions.create.call_count == 0

    # Case B: Delayed 'Jaipur' (message_id=40, >2 gap and >45m old) routes straight to AgentCore (zero Decisions drop risk!)
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
    mock_agent_run = AsyncMock(return_value={"kind": "direct_reply", "reply_text": "Noted.", "goals": []})
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=Settings(telegram_burst_debounce_seconds=0.0)),
        patch("app.services.decisions_triage._get_decisions_client", return_value=fake_dec_client),
        patch("app.routers.telegram.agent_core.run_agent_turn", mock_agent_run),
        patch("app.routers.telegram.telegram_api.send_message", AsyncMock(return_value={"result": {"message_id": 41}})),
    ):
        await tg_router.process_debounced_burst(chat_id, 1001, wait_locally=False)

    assert sb.store["telegram_inbox_buffer"][0]["status"] == "processed"
    mock_agent_run.assert_awaited_once()
    assert fake_dec_client.decisions.create.call_count == 0


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
