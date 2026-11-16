"""Comprehensive unit tests for AgentCore Goal Intelligence upgrades (#2, #3, #4, #6)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.auth import CurrentUser, get_current_user
from app.config import Settings, get_settings
from app.main import app
from app.services import agent_core, goal_intelligence
from app.services.agent_core import AgentTurnEnvelope


class FakeTable:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def _q(self) -> "FakeQuery":
        return FakeQuery(self.rows)

    def select(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
        return self._q().select()

    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> "FakeQuery":
        return self._q().insert(payload)

    def update(self, patch_obj: dict[str, Any]) -> "FakeQuery":
        return self._q().update(patch_obj)

    def delete(self) -> "FakeQuery":
        return self._q().delete()


class FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._op = "select"
        self._payload: Any = None
        self._filters: list[Any] = []
        self._limit: int | None = None
        self._order_col: str | None = None
        self._order_desc: bool = False

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

    def order(self, col: str = "created_at", desc: bool = False, **_kwargs: Any) -> "FakeQuery":
        self._order_col = col
        self._order_desc = bool(desc)
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._limit = n
        return self

    def execute(self) -> SimpleNamespace:
        matched = [r for r in self._rows if all(fn(r) for fn in self._filters)]
        if self._op == "select" and self._order_col:
            matched.sort(
                key=lambda r: str(r.get(self._order_col) or ""),
                reverse=self._order_desc,
            )
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
                    "id": "p-lala",
                    "full_name": "Lala Ji",
                    "person_type": "salaried_karigar",
                    "role_tags": ["karigar"],
                    "aliases": [],
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
            "project_items": [
                {
                    "id": "item-kitchen",
                    "project_id": "prj-ambure",
                    "item_name": "Kitchen",
                    "stage": "in_shop",
                    "production_mistri_id": "p-lala",
                }
            ],
            "stage_events": [],
            "agent_goals": [],
            "workshop_memory": [],
            "vendors": [],
        }
    )


# ---------------------------------------------------------------------------
# Feature #2: Post-Write Self-Verification (Happy Path + Drift Detection)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature2_post_write_verification_happy_and_drift_detection():
    sb = _seed_fake_sb()
    sb.store["agent_goals"].append(
        {
            "id": "goal-verify-ok",
            "owner_person_id": "p-shreyansh",
            "goal_type": "stage_update",
            "status": "awaiting_confirmation",
            "summary_title": "Move Ambure Ji to Execution",
            "collected_slots": {
                "operation_kind": "pipeline_stage_move",
                "project_id": "prj-ambure",
                "to_stage": "execution",
            },
            "steps": [
                {
                    "step_uuid": "s-1",
                    "step_index": 0,
                    "target": "supabase",
                    "operation": "pipeline_stage_move",
                    "status": "pending",
                }
            ],
        }
    )

    with patch("app.services.telegram_api.send_message", new_callable=AsyncMock):
        res_ok = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-verify-ok",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res_ok["status"] == "completed"
    assert res_ok["verified"] is True
    assert sb.store["agent_goals"][0]["metadata"]["verified_checks"] == 1

    # Case B: Silent DB drift caught before marking completed
    sb.store["agent_goals"].append(
        {
            "id": "goal-verify-drift",
            "owner_person_id": "p-shreyansh",
            "goal_type": "stage_update",
            "status": "awaiting_confirmation",
            "summary_title": "Move Ambure Ji Kitchen to Packed",
            "collected_slots": {
                "operation_kind": "item_upsert_or_move",
                "project_id": "prj-ambure",
                "item_id": "item-kitchen",
                "to_stage": "packed_ready",
            },
            "steps": [
                {
                    "step_uuid": "s-2",
                    "step_index": 0,
                    "target": "supabase",
                    "operation": "item_upsert_or_move",
                    "status": "pending",
                }
            ],
        }
    )

    async def _drifted_step(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # Simulate step claiming success without actually mutating stage to 'packed_ready'
        return {"ok": True, "item_id": "item-kitchen", "receipt": "Kitchen moved"}

    with patch("app.services.agent_core._run_single_step", side_effect=_drifted_step):
        res_drift = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-verify-drift",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res_drift["status"] == "needs_error_fix"
    assert res_drift["can_park_for_clerk"] is True
    assert "packed_ready" in res_drift["prompt"]
    assert sb.store["agent_goals"][1]["status"] == "needs_error_fix"

    # Case C: Non-draft supplier bill caught by Draft-Only invariant check
    sb.store["agent_goals"].append(
        {
            "id": "goal-verify-nondraft",
            "owner_person_id": "p-shreyansh",
            "goal_type": "supplier_bill",
            "status": "awaiting_confirmation",
            "summary_title": "Patel Ply Bill",
            "collected_slots": {
                "operation_kind": "supplier_draft_bill",
                "party_name": "Patel Ply",
                "amount_inr": 15000,
                "invoice_no": "INV-99",
            },
            "steps": [
                {
                    "step_uuid": "s-3",
                    "step_index": 0,
                    "target": "zoho",
                    "operation": "supplier_draft_bill",
                    "status": "pending",
                }
            ],
        }
    )

    with (
        patch("app.services.zoho.check_duplicate_bill", new=AsyncMock(return_value=None)),
        patch(
            "app.services.zoho.resolve_or_create_contact",
            new=AsyncMock(return_value={"status": "resolved", "contact_id": "v-1", "contact_name": "Patel Ply"}),
        ),
        patch(
            "app.services.zoho.create_draft_bill",
            new=AsyncMock(return_value={"stubbed": False, "bill": {"status": "open"}}),
        ),
    ):
        res_nondraft = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-verify-nondraft",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res_nondraft["status"] == "needs_error_fix"
    assert "draft" in res_nondraft["prompt"].lower()


# ---------------------------------------------------------------------------
# Feature #3: 1-Line Parked-Goal Nudge After Side-Quests (Per-Operator Scoped)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature3_side_quest_parked_goal_nudge_and_isolation():
    sb = _seed_fake_sb()
    sb.store["agent_goals"].append(
        {
            "id": "goal-parked-old",
            "owner_person_id": "p-shreyansh",
            "goal_type": "financial_txn",
            "status": "gathering_info",
            "summary_title": "Kaushab ₹1L Receipt",
            "pending_prompt": "Which account (Cash Galla / Axis Bank) received ₹1,00,000?",
            "collected_slots": {"operation_kind": "customer_receipt", "amount_inr": 100000},
            "steps": [],
        }
    )

    # Shreyansh executes an unrelated unambiguous side-quest (Kitchen -> packed_ready)
    side_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="item_upsert_or_move",
                summary_title="Pack Ambure Kitchen",
                project_id="prj-ambure",
                item_id="item-kitchen",
                to_stage="packed_ready",
            )
        ],
    ).model_dump_json()

    fake_client = MagicMock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_side", steps=[], output_text=side_env
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out_p1 = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="production",
            text="Ambure ji ka kitchen pack ho gaya",
        )

    assert out_p1["goals"][0]["status"] == "completed"
    assert out_p1["parked_goal_nudge"] is not None
    assert out_p1["parked_goal_nudge"]["goal_id"] == "goal-parked-old"
    assert "📌 *Still open (Kaushab ₹1L Receipt):*" in out_p1["goals"][0]["receipt"]
    # Old goal remains untouched in gathering_info
    assert sb.store["agent_goals"][0]["status"] == "gathering_info"

    # Operator P2 (Ansh) must NOT see Shreyansh's parked goal nudge
    qa_env = AgentTurnEnvelope(
        intent_category="direct_answer",
        direct_reply_text="Ambure order material check is in progress.",
        goal_items=[],
    ).model_dump_json()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_p2", steps=[], output_text=qa_env
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out_p2 = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-ansh",
            actor_label="Ansh",
            source_channel="telegram",
            domain_scope="production",
            text="Ambure status kya hai?",
        )
    assert out_p2["parked_goal_nudge"] is None
    assert "Still open" not in out_p2["reply_text"]

    # Current turn asking a question (status == 'gathering_info') must NOT stack a second nudge
    ask_env = AgentTurnEnvelope(
        intent_category="goal_actions",
        goal_items=[
            agent_core.GoalIntentItem(
                operation_kind="expense",
                summary_title="Tea Expense ₹500",
                amount_inr=500,
                is_ambiguous=False,
                clarification_question="Which account paid ₹500?",
            )
        ],
    ).model_dump_json()
    fake_client.interactions.create.return_value = SimpleNamespace(
        id="int_ask", steps=[], output_text=ask_env
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_client),
    ):
        out_ask = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Chai ke 500 kharch kiye",
        )
    assert out_ask["goals"][0]["status"] == "gathering_info"
    assert out_ask["parked_goal_nudge"] is None


# ---------------------------------------------------------------------------
# Feature #4: Auto Background Zoho Retry + Exhaustion + /tasks/goal-retry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature4_zoho_transient_retry_worker_and_exhaustion():
    sb = _seed_fake_sb()
    sb.store["agent_goals"].append(
        {
            "id": "goal-retry-1",
            "owner_person_id": "p-shreyansh",
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Expense ₹2,500 (Axis)",
            "telegram_chat_id": 1001,
            "telegram_bot_msg_id": 777,
            "collected_slots": {
                "operation_kind": "expense",
                "amount_inr": 2500,
                "payment_account_id": "acc-axis",
                "party_name": "Tempo Freight",
            },
            "steps": [
                {
                    "step_uuid": "s-r1",
                    "step_index": 0,
                    "target": "zoho",
                    "operation": "expense",
                    "status": "pending",
                    "retry_count": 0,
                }
            ],
            "metadata": {},
        }
    )

    with (
        patch(
            "app.services.zoho.push_management_entry_to_zoho",
            new=AsyncMock(
                return_value={
                    "ok": False,
                    "status_code": 503,
                    "error_kind": "transient_5xx",
                    "error": {"message": "Zoho 503 Service Unavailable"},
                }
            ),
        ),
        patch(
            "app.services.goal_intelligence.enqueue_goal_retry_task",
            return_value={"queued": True, "mode": "cloud_tasks_rest", "delay_seconds": 15.0},
        ) as mock_enqueue,
    ):
        res1 = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-retry-1",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res1["status"] == "transient_retry_pending"
    assert res1["auto_retry_attempt"] == 1
    assert res1["auto_retry_queued"] is True
    assert sb.store["agent_goals"][0]["status"] == "awaiting_confirmation"
    mock_enqueue.assert_called_once()
    assert mock_enqueue.call_args.kwargs["attempt"] == 1

    # Verify /tasks/goal-retry secret enforcement and successful background recovery
    app.dependency_overrides[get_settings] = lambda: Settings(
        cron_auth_secret="cron-secret-123",
        telegram_bot_token="bot-tok",
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch("app.routers.telegram.get_supabase_admin", return_value=sb):
                bad = await client.post(
                    "/tasks/goal-retry",
                    headers={"X-Cron-Secret": "wrong"},
                    json={"goal_id": "goal-retry-1"},
                )
                assert bad.status_code == 403

                with (
                    patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
                    patch("app.services.goal_intelligence.get_settings", return_value=Settings(telegram_bot_token="bot-tok")),
                    patch("app.services.telegram_api.edit_message_text", new_callable=AsyncMock) as mock_tg_edit,
                ):
                    ok = await client.post(
                        "/tasks/goal-retry",
                        headers={"X-Cron-Secret": "cron-secret-123"},
                        json={"goal_id": "goal-retry-1", "actor_label": "Auto-Retry"},
                    )
                assert ok.status_code == 200
                body = ok.json()
                assert body["status"] == "completed"
                assert sb.store["agent_goals"][0]["status"] == "completed"
                mock_tg_edit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()

    # Retry exhaustion after MAX_AUTO_ZOHO_RETRIES (attempt 4 -> needs_error_fix)
    sb.store["agent_goals"].append(
        {
            "id": "goal-retry-maxed",
            "owner_person_id": "p-shreyansh",
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Expense ₹3,000",
            "collected_slots": {
                "operation_kind": "expense",
                "amount_inr": 3000,
                "payment_account_id": "acc-axis",
                "party_name": "Tempo",
            },
            "steps": [
                {
                    "step_uuid": "s-r4",
                    "step_index": 0,
                    "target": "zoho",
                    "operation": "expense",
                    "status": "failed_transient",
                    "retry_count": 3,
                }
            ],
            "metadata": {},
        }
    )

    with patch(
        "app.services.zoho.push_management_entry_to_zoho",
        new=AsyncMock(
            return_value={
                "ok": False,
                "status_code": 429,
                "error_kind": "transient_5xx",
                "error": {"message": "429 Too Many Requests"},
            }
        ),
    ):
        res4 = await agent_core.confirm_and_execute_goal(
            sb,
            goal_id="goal-retry-maxed",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
        )

    assert res4["status"] == "needs_error_fix"
    assert res4["can_park_for_clerk"] is True
    assert sb.store["agent_goals"][1]["status"] == "needs_error_fix"


def test_feature4_enqueue_cloud_tasks_v2_rest_payload():
    mock_creds = SimpleNamespace(token="gcp-tok", refresh=MagicMock())
    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()

    with (
        patch(
            "app.services.goal_intelligence.get_settings",
            return_value=Settings(
                _env_file=None,
                app_env="production",
                gcp_project_id="beawar-os",
                gcp_region="asia-south1",
                cloud_tasks_queue="munim-q",
                app_base_url="https://munim.run.app",
                cron_auth_secret="cron-sec",
            ),
        ),
        patch("google.auth.default", return_value=(mock_creds, "beawar-os")),
        patch("httpx.post", return_value=mock_post_resp) as mock_httpx_post,
    ):
        out = goal_intelligence.enqueue_goal_retry_task(
            goal_id="g-123",
            actor_person_id="p-shreyansh",
            actor_label="Shreyansh",
            attempt=2,
        )

    assert out["queued"] is True
    assert out["delay_seconds"] == 60.0
    call_url = mock_httpx_post.call_args.args[0]
    assert "/projects/beawar-os/locations/asia-south1/queues/munim-q/tasks" in call_url
    task_body = mock_httpx_post.call_args.kwargs["json"]["task"]
    assert task_body["httpRequest"]["url"] == "https://munim.run.app/tasks/goal-retry"
    assert task_body["httpRequest"]["headers"]["X-Cron-Secret"] == "cron-sec"


# ---------------------------------------------------------------------------
# Feature #6: Procedural Memory Pre-Gate, Distillation & Master Alias Append
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature6_procedural_memory_pregate_and_alias_distillation():
    sb = _seed_fake_sb()
    sb.store["agent_goals"].append(
        {
            "id": "goal-mem-1",
            "owner_person_id": "p-shreyansh",
            "goal_type": "financial_txn",
            "status": "awaiting_confirmation",
            "summary_title": "Lala Ji Payout ₹5,000",
            "collected_slots": {
                "operation_kind": "vendor_payment",
                "person_id": "p-lala",
                "party_name": "Lala Ji",
                "amount_inr": 5000,
                "payment_account_code": "axis",
                "payment_account_id": "acc-axis",
            },
            "steps": [],
        }
    )

    # 1. One-off amount_inr edit -> skipped deterministically with ZERO Gemini API calls!
    with (
        patch("app.services.decisions_triage.should_distill_memory_rule") as mock_gate,
        patch("app.services.goal_intelligence._get_interactions_client") as mock_hy_client,
    ):
        one_off = agent_core.edit_open_goal_slots(
            sb,
            goal_id="goal-mem-1",
            slot_patch={"amount_inr": 5500},
            correction_note="Actually 5500 hai",
        )
    assert one_off["memory_distilled"]["saved"] is False
    assert one_off["memory_distilled"]["reason"] == "one_off_transactional_edit"
    mock_gate.assert_not_called()
    mock_hy_client.assert_not_called()

    # 2. Reusable proxy alias correction -> Decisions gate -> Hydrogen (low) -> workshop_memory + people.payout_aliases
    distilled_json = json.dumps(
        {
            "should_save": True,
            "rule_text": "Lala Ji bank transfers go to Ruksar Bano",
            "category": "entity",
            "alias_target_table": "people",
            "alias_target_id": "p-lala",
            "alias_value": "Ruksar Bano",
            "is_payout_alias": True,
        }
    )
    fake_hy_client = MagicMock()
    fake_hy_client.interactions.create.return_value = SimpleNamespace(
        id="int_mem", steps=[], output_text=distilled_json
    )

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        auth_type="telegram", identifier="1001", display_name="Shreyansh"
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch("app.routers.domains.get_supabase_admin", return_value=sb),
                patch("app.services.decisions_triage.should_distill_memory_rule", return_value=True) as mock_gate_ok,
                patch("app.services.goal_intelligence.get_settings", return_value=Settings(gemini_api_key="test-key")),
                patch("app.services.goal_intelligence._get_interactions_client", return_value=fake_hy_client),
            ):
                r1 = await client.post(
                    "/api/agent/goals/goal-mem-1/edit",
                    json={
                        "slot_patch": {"party_name": "Ruksar Bano"},
                        "correction_note": "Lala Ji transfers always go to Ruksar Bano",
                    },
                )
                # Second identical correction verifies idempotent deduplication
                r2 = await client.post(
                    "/api/agent/goals/goal-mem-1/edit",
                    json={
                        "slot_patch": {"payout_aliases": ["Ruksar Bano"]},
                        "correction_note": "Lala Ji transfers always go to Ruksar Bano",
                    },
                )
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == 200
    assert r2.status_code == 200
    b1 = r1.json()
    assert b1["memory_distilled"]["saved"] is True
    assert b1["memory_distilled"]["alias_updated"] is True
    mock_gate_ok.assert_called()
    assert fake_hy_client.interactions.create.call_args.kwargs["generation_config"] == {
        "thinking_level": "low"
    }
    assert len(sb.store["workshop_memory"]) == 1
    assert sb.store["workshop_memory"][0]["rule_text"] == "Lala Ji bank transfers go to Ruksar Bano"
    assert sb.store["people"][2]["payout_aliases"] == ["Ruksar Bano"]


@pytest.mark.asyncio
async def test_schema_slimming_active_orders_grounding_and_empty_goal_guard():
    schema = agent_core._inline_json_schema(
        AgentTurnEnvelope, force_required=("goal_items",)
    )
    assert "goal_items" in schema.get("required", [])
    dumped = json.dumps(schema)
    assert '"$defs"' not in dumped
    assert '"$ref"' not in dumped
    # Nullable anyOf wrappers should be collapsed cleanly to single types
    goal_props = schema["properties"]["goal_items"]["items"]["properties"]
    assert goal_props["project_id"]["type"] == "string"
    assert "anyOf" not in goal_props["project_id"]

    sb = _seed_fake_sb()
    ctx_json, _ = agent_core._build_compact_grounding(
        sb, owner_person_id="p-shreyansh", domain_scope="production", ui_context={}
    )
    ctx_obj = json.loads(ctx_json)
    assert len(ctx_obj["active_orders_index"]) == 1
    assert ctx_obj["active_orders_index"][0]["project_code"] == "PRJ-41"
    assert len(ctx_obj["active_orders_index"][0]["items"]) == 1

    # Case A: Gemini initially returns goal_actions without goal_items -> retry populates & commits!
    recovered_env = AgentTurnEnvelope(
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

    fake_retry_client = MagicMock()
    fake_retry_client.interactions.create.side_effect = [
        SimpleNamespace(id="int_empty_1", steps=[], output_text='{"intent_category":"goal_actions"}'),
        SimpleNamespace(id="int_recovered_2", steps=[], output_text=recovered_env),
    ]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_retry_client),
        patch("app.services.telegram_api.send_message", new_callable=AsyncMock),
    ):
        rec_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="production",
            text="Move Ambure Ji to execution",
        )

    assert rec_out["kind"] == "goal_actions"
    assert rec_out["goals"][0]["status"] == "completed"
    assert rec_out["goals"][0]["project_id"] == "prj-ambure"

    # Case B: Still empty after retry -> returns clean is_error=True (never an empty yellow card!)
    fake_empty_client = MagicMock()
    fake_empty_client.interactions.create.side_effect = [
        SimpleNamespace(id="int_e1", steps=[], output_text='{"intent_category":"goal_actions"}'),
        SimpleNamespace(id="int_e2", steps=[], output_text='{"intent_category":"goal_actions"}'),
    ]

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_empty_client),
    ):
        err_out = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="production",
            text="Do the impossible thing",
        )

    assert err_out["kind"] == "unrecognized"
    assert err_out["is_error"] is True
    assert err_out["goals"] == []
    assert err_out["reply_text"]


@pytest.mark.asyncio
async def test_rolling_10_turn_30_min_memory_and_enriched_orders_grounding():
    from datetime import datetime, timedelta, timezone

    sb = _seed_fake_sb()

    # 1. Verify enriched active_orders_index contains ₹ quote / paid / pending
    ctx_init, _ = agent_core._build_compact_grounding(
        sb,
        owner_person_id="p-shreyansh",
        domain_scope="production",
        ui_context={},
        source_channel="web_command_bar",
    )
    init_obj = json.loads(ctx_init)
    ord_entry = init_obj["active_orders_index"][0]
    assert ord_entry["quoted_total"] == 2300000.0
    assert ord_entry["paid_inr"] == 300000.0
    assert ord_entry["balance_pending_inr"] == 2000000.0
    assert init_obj["recent_conversation_turns"] == []

    # 2. Turn 1 (on Munim tab): ask about Ambure payments -> persisted cleanly as goal_type='query'
    qa1_env = AgentTurnEnvelope(
        intent_category="direct_answer",
        direct_reply_text="Ambure Ji ne ₹3,00,000 diye hain, ₹20,00,000 baki hain.",
        referenced_project_id="prj-ambure",
        referenced_customer_id="c-ambure",
    ).model_dump_json()

    fake_qa_client = MagicMock()
    fake_qa_client.interactions.create.return_value = SimpleNamespace(
        id="int_qa1", steps=[], output_text=qa1_env
    )

    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_qa_client),
    ):
        out1 = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="web_command_bar",
            domain_scope="munim",
            text="Ambure Ji ka kitna paisa baki hai?",
        )

    assert "₹3,00,000" in out1["reply_text"]
    assert len(sb.store["agent_goals"]) == 1
    saved_q = sb.store["agent_goals"][0]
    assert saved_q["goal_type"] == "query"
    assert saved_q["status"] == "completed"
    assert saved_q["project_id"] == "prj-ambure"

    # 3. Turn 2 (crossed to Production tab 20s later): grounding includes Turn 1 in recent_conversation_turns!
    ctx_turn2, _ = agent_core._build_compact_grounding(
        sb,
        owner_person_id="p-shreyansh",
        domain_scope="production",
        ui_context={},
        source_channel="web_command_bar",
    )
    t2_obj = json.loads(ctx_turn2)
    assert len(t2_obj["recent_conversation_turns"]) == 1
    assert t2_obj["recent_conversation_turns"][0]["project_id"] == "prj-ambure"
    assert "Ambure Ji" in t2_obj["recent_conversation_turns"][0]["user_said"]

    # 4. Idle >30m resets context completely; 12 fresh turns cap cleanly to latest 10 in chronological order
    now = datetime.now(timezone.utc)
    stale_sb = FakeSupabase(
        {
            "agent_goals": [
                {
                    "id": "old-q",
                    "owner_person_id": "p-shreyansh",
                    "source_channel": "web_command_bar",
                    "goal_type": "query",
                    "status": "completed",
                    "summary_title": "Q: Old stale topic",
                    "collected_slots": {"user_message": "Old stale", "assistant_reply": "Old reply"},
                    "turn_digest": [{"user": "Old stale", "assistant": "Old reply"}],
                    "updated_at": (now - timedelta(minutes=45)).isoformat(),
                    "created_at": (now - timedelta(minutes=45)).isoformat(),
                }
            ]
        }
    )
    assert (
        goal_intelligence.load_rolling_conversation_context(
            stale_sb,
            owner_person_id="p-shreyansh",
            source_channel="web_command_bar",
            now_utc=now,
        )
        == []
    )

    cap_rows = []
    for idx in range(12):
        ts = (now - timedelta(minutes=12 - idx)).isoformat()
        cap_rows.append(
            {
                "id": f"q-{idx}",
                "owner_person_id": "p-shreyansh",
                "source_channel": "web_command_bar",
                "goal_type": "query",
                "status": "completed",
                "summary_title": f"Q: Turn {idx}",
                "collected_slots": {"user_message": f"Turn {idx}", "assistant_reply": f"Ans {idx}"},
                "turn_digest": [{"user": f"Turn {idx}", "assistant": f"Ans {idx}"}],
                "updated_at": ts,
                "created_at": ts,
            }
        )
    cap_sb = FakeSupabase({"agent_goals": cap_rows})
    capped = goal_intelligence.load_rolling_conversation_context(
        cap_sb,
        owner_person_id="p-shreyansh",
        source_channel="web_command_bar",
        now_utc=now,
    )
    assert len(capped) == 10
    assert capped[0]["user_said"] == "Turn 2"
    assert capped[-1]["user_said"] == "Turn 11"


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

