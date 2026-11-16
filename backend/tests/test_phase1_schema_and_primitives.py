from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from unittest.mock import MagicMock

from app.services.goal_primitives import (
    AgentGoalChecklist,
    CheckpointedStep,
    ITEM_EXECUTION_STAGES,
    MATERIAL_TRACK_STAGES,
    PAYMENT_TRACK_STAGES,
    PROJECT_PIPELINE_STAGES,
    build_compact_open_goals_tray,
    build_stage_undo_snapshot,
    build_zoho_standard_order_tags,
    claim_goal_for_execution,
    classify_external_error,
    compute_ist_attendance_minutes,
    compute_missing_customer_onboarding_slots,
    format_inr,
    format_voice_receipt_header,
    make_zoho_reference_number,
    merge_buffered_telegram_burst,
    select_hydrogen_thinking_level,
    should_run_decisions_front_door,
)

MIGRATION_006 = (
    Path(__file__).resolve().parents[2]
    / "supabase"
    / "migrations"
    / "006_unified_multi_domain_and_agent.sql"
)


def test_migration_006_enforces_all_supabase_security_invariants():
    sql = MIGRATION_006.read_text(encoding="utf-8")
    non_comment_sql = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).lower()

    # 1. Forbidden constructs per GEMINI.md (checked on non-comment SQL)
    assert "security definer" not in non_comment_sql
    assert "auth.role()" not in non_comment_sql
    assert "user_metadata" not in non_comment_sql
    assert "create type " not in non_comment_sql

    # 2. Zero silent expiry on agent_goals (Hardening #4)
    agent_goals_block = sql.split("CREATE TABLE IF NOT EXISTS public.agent_goals")[1].split(");")[0]
    assert "expires_at" not in agent_goals_block
    assert "'expired'" not in agent_goals_block
    assert "heard_transcript" in agent_goals_block

    # 3. Every new public table enables RLS
    expected_rls_tables = (
        "project_pipeline_stages",
        "material_track_stages",
        "payment_track_stages",
        "customers",
        "vendors",
        "people",
        "payment_accounts",
        "stage_events",
        "telegram_webhook_dedup",
        "telegram_inbox_buffer",
        "agent_goals",
        "worker_attendance",
        "monthly_fnf_sheets",
        "worker_payroll_entries",
    )
    for tbl in expected_rls_tables:
        assert f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY;" in sql

    # 4. Every SQL view uses WITH (security_invoker = true)
    for view_name in ("v_project_pnl", "v_whos_doing_what", "v_needs_info_queue"):
        idx = sql.index(f"VIEW public.{view_name}")
        snippet = sql[idx : idx + 160].lower()
        assert "with (security_invoker = true)" in snippet

    # 5. Locked stage codes & Zoho Standard Reporting Tag fields present
    for code in PROJECT_PIPELINE_STAGES:
        assert f"'{code}'" in sql
    for code in MATERIAL_TRACK_STAGES:
        assert f"'{code}'" in sql
    for code in PAYMENT_TRACK_STAGES:
        assert f"'{code}'" in sql
    for code in ITEM_EXECUTION_STAGES:
        assert f"'{code}'" in sql
    assert "zoho_tag_option_id" in sql
    assert "cf_project_code" in sql


def test_surgical_decisions_gate_and_explicit_thinking_levels():
    # DM or Web Command Bar plain text -> skip serial Decisions API hop!
    assert (
        should_run_decisions_front_door(
            is_group_chat=False,
            is_bot_mentioned_or_reply=False,
            has_photo_or_pdf=False,
            domain_channel="production",
        )
        is False
    )
    # Unaddressed group chat text -> filter via cheap gemini-decision-1!
    assert (
        should_run_decisions_front_door(
            is_group_chat=True,
            is_bot_mentioned_or_reply=False,
            has_photo_or_pdf=False,
            domain_channel="munim",
        )
        is True
    )
    # Photo upload or Attendance photo -> triage via gemini-decision-1!
    assert (
        should_run_decisions_front_door(
            is_group_chat=False,
            is_bot_mentioned_or_reply=True,
            has_photo_or_pdf=True,
            domain_channel="munim",
        )
        is True
    )

    assert select_hydrogen_thinking_level() == "low"
    assert select_hydrogen_thinking_level(is_multi_item_dump=True) == "medium"
    assert select_hydrogen_thinking_level(is_error_diagnosis=True) == "medium"
    assert select_hydrogen_thinking_level(ocr_difficulty_score=1.8) == "high"


def test_burst_merging_and_atomic_compare_and_swap():
    merged = merge_buffered_telegram_burst(
        [
            {
                "telegram_update_id": 102,
                "raw_payload": {"message": {"message_id": 52, "text": "Ambure ji Jaipur site"}},
            },
            {
                "telegram_update_id": 100,
                "raw_payload": {
                    "message": {
                        "message_id": 50,
                        "photo": [{"file_id": "low"}, {"file_id": "hi_res_1"}],
                    }
                },
            },
            {
                "telegram_update_id": 101,
                "raw_payload": {"message": {"message_id": 51, "text": "Patel ply ka bill hai"}},
            },
        ]
    )
    assert merged["update_count"] == 3
    assert merged["photo_file_ids"] == ["hi_res_1"]
    assert merged["combined_text"] == "Patel ply ka bill hai\nAmbure ji Jaipur site"
    assert merged["latest_message_id"] == 52

    # Atomic CAS: first tap claims goal, duplicate 4G tap gets None
    sb_first = MagicMock()
    sb_first.table.return_value.update.return_value.eq.return_value.in_.return_value.execute.return_value.data = [
        {"id": "goal-1", "status": "executing"}
    ]
    assert claim_goal_for_execution(sb_first, "goal-1") == {"id": "goal-1", "status": "executing"}

    sb_dup = MagicMock()
    sb_dup.table.return_value.update.return_value.eq.return_value.in_.return_value.execute.return_value.data = []
    assert claim_goal_for_execution(sb_dup, "goal-1") is None


def test_customer_onboarding_slots_and_compound_goal_tray():
    assert compute_missing_customer_onboarding_slots({"full_name": "Manoj Sharma"}) == [
        "area_landmark",
        "rough_scope",
    ]
    assert (
        compute_missing_customer_onboarding_slots(
            {
                "full_name": "Manoj Sharma",
                "area_landmark": "Ajmer Road",
                "rough_scope": "Modular Kitchen + 2 Wardrobes",
            }
        )
        == []
    )

    goal = AgentGoalChecklist(
        goal_type="new_lead",
        summary_title="New Lead: Sharma Ji",
        missing_slots=["area_landmark"],
        pending_prompt="Share Site Area / Landmark for Sharma Ji",
        steps=[CheckpointedStep(step_index=0, target="supabase", operation="create_customer")],
    )
    tray = build_compact_open_goals_tray([goal.model_dump()])
    assert len(tray) == 1
    assert tray[0]["missing_slots"] == ["area_landmark"]


def test_zoho_standard_tags_idempotency_and_error_split():
    ref = make_zoho_reference_number("a1b2c3d4-e5f6-7890-abcd-ef1234567890", 2)
    assert ref == "MUNIM-A1B2C3D4E5F67890-S2"
    assert len(ref) <= 36

    tags_payload = build_zoho_standard_order_tags(
        project_code="PRJ-261116-01",
        zoho_tag_id="tag_99",
        zoho_tag_option_id="opt_41",
    )
    assert tags_payload["tags"] == [{"tag_id": "tag_99", "tag_option_id": "opt_41"}]
    assert tags_payload["custom_fields"] == [
        {"api_name": "cf_project_code", "value": "PRJ-261116-01"}
    ]

    assert classify_external_error(503) == "transient_5xx"
    assert classify_external_error(429) == "transient_5xx"
    assert classify_external_error(None, is_timeout=True) == "transient_5xx"
    assert classify_external_error(400) == "validation_400"
    assert classify_external_error(200) == "none"


def test_voice_echo_inr_format_undo_snapshot_and_attendance_ot():
    assert (
        format_voice_receipt_header("Ambure ji se 5 lakh cash aaya")
        == '🎙️ Heard: _"Ambure ji se 5 lakh cash aaya"_\n'
    )
    assert format_voice_receipt_header(None) == ""

    assert format_inr(2300000) == "₹23,00,000"
    assert format_inr(115000.5) == "₹1,15,000.50"
    assert format_inr(749) == "₹749"

    snap = build_stage_undo_snapshot(
        track="item",
        row_before={
            "stage": "in_shop",
            "stage_entered_at": "2026-11-16T05:00:00Z",
            "production_mistri_id": "u-ramesh",
            "assembly_mistri_id": None,
            "qc_passed_by_id": None,
            "qc_passed_at": None,
        },
    )
    assert snap["stage"] == "in_shop"
    assert snap["production_mistri_id"] == "u-ramesh"

    # Worker checks in 09:30 IST (04:00 UTC) and exits 19:45 IST (14:15 UTC) -> 10h15m worked, 1h45m (>6 PM) OT
    worked, ot = compute_ist_attendance_minutes(
        datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 16, 14, 15, tzinfo=timezone.utc),
        shift_end_ist=dt_time(18, 0),
    )
    assert worked == 615
    assert ot == 105


def test_cloud_tasks_rest_dispatcher_supports_debounce_schedule_time():
    import base64
    import json
    from unittest.mock import patch
    import httpx
    from app.config import Settings
    from app.routers.telegram import _dispatch_to_cloud_tasks

    cfg = Settings(
        gcp_project_id="proj-1",
        gcp_region="asia-south1",
        cloud_tasks_queue="q-1",
        app_base_url="https://munim.example.com",
        cron_auth_secret="sec-123",
    )
    fake_creds = MagicMock()
    fake_creds.token = "ya29.fake-token"
    fake_resp = httpx.Response(
        200,
        json={"name": "projects/proj-1/locations/asia-south1/queues/q-1/tasks/t1"},
        request=httpx.Request("POST", "https://cloudtasks.googleapis.com/v2/test"),
    )

    with (
        patch("google.auth.default", return_value=(fake_creds, "proj-1")),
        patch("httpx.post", return_value=fake_resp) as mock_post,
    ):
        _dispatch_to_cloud_tasks({"update_id": 777}, cfg, delay_seconds=2.5)

    assert mock_post.call_count == 1
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer ya29.fake-token"
    task_payload = call_kwargs["json"]["task"]
    assert "scheduleTime" in task_payload
    decoded = json.loads(base64.b64decode(task_payload["httpRequest"]["body"]).decode("utf-8"))
    assert decoded == {"update_id": 777}

