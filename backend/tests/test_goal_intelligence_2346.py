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


def test_financial_slot_backfill_extracts_amount_and_cash_account():
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {"code": "cash_galla", "display_name": "Cash Galla", "aliases": ["cash", "galla", "nakad"], "is_active": True},
                {"code": "rgb", "display_name": "RGB (Gramin Bank)", "aliases": ["rgb", "gramin bank"], "is_active": True},
            ],
            "projects": [
                {"id": "7923bc50-77dc-4779-9a73-61cf722c318e", "customer_name": "Praveen Mehta"}
            ],
        }
    )
    slots = {
        "operation_kind": "customer_receipt",
        "project_id": "7923bc50-77dc-4779-9a73-61cf722c318e",
        "summary_title": "Praveen Mehta se ₹20,000 advance Cash Galla mein receive",
    }
    agent_core._backfill_financial_slots_from_text(
        sb,
        slots,
        user_text="Praveen Mehta Advance Received 20,000 cash",
        heard_transcript=None,
    )
    assert slots["amount_inr"] == 20000.0
    assert slots["payment_account_code"] == "cash_galla"
    assert slots["party_name"] == "Praveen Mehta"

    slots_k = {"operation_kind": "customer_receipt", "summary_title": "Received 50k in RGB"}
    agent_core._backfill_financial_slots_from_text(
        sb, slots_k, user_text="50k rgb", heard_transcript=None
    )
    assert slots_k["amount_inr"] == 50000.0
    assert slots_k["payment_account_code"] == "rgb"


@pytest.mark.asyncio
async def test_item_materials_crud_track_sync_undo_agent_and_rest_api():
    from app.services import step_executors

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
    from app.services import step_executors

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


# ===========================================================================
# Pass 1 Audit Verification Tests
# ===========================================================================
@pytest.mark.asyncio
async def test_pass1_zoho_reporting_tags_draft_bill_and_pnl_sync():
    from app.services import zoho

    pid = "11111111-1111-1111-1111-111111111101"
    cid = "22222222-2222-2222-2222-222222222201"
    sb = FakeSupabase(
        {
            "customers": [
                {"id": cid, "full_name": "Mehta Garment", "area_landmark": "Sendra Road", "phone": "9829000001"}
            ],
            "projects": [
                {
                    "id": pid,
                    "project_code": "PRJ-101",
                    "cf_project_code": "PRJ-101",
                    "customer_id": cid,
                    "customer_name": "Mehta Garment",
                    "phone": "9829000001",
                    "status": "active",
                    "project_type": "customer",
                    "zoho_contact_id": "zc-1",
                    "zoho_tag_id": None,
                    "zoho_tag_option_id": None,
                    "zoho_project_id": None,
                }
            ],
            "entity_dictionary": [],
        }
    )

    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
        zoho_project_tag_id="",
    )

    async def fake_zoho_request(client, method, path, *, params=None, json_body=None):
        assert "/settings/tags" not in path
        if method == "GET" and path == "/reportingtags":
            return httpx.Response(200, json={"reporting_tags": [{"tag_id": "tag-prj", "tag_name": "Project", "is_active": True}]})
        if method == "GET" and path == "/reportingtags/tag-prj":
            return httpx.Response(200, json={"reporting_tag": {"tag_id": "tag-prj", "tag_options": []}})
        if method == "GET" and path == "/reportingtags/tag-prj/options/all":
            return httpx.Response(200, json={"tag_options": []})
        if method == "POST" and path == "/reportingtags/tag-prj/options":
            return httpx.Response(200, json={"tag_option": {"tag_option_id": "opt-101", "tag_option_name": "PRJ-101 - Mehta Garment"}})
        if method == "GET" and path == "/bills" and (params or {}).get("reference_number") == "MUNIM-G1-S1":
            return httpx.Response(200, json={"bills": []})
        if method == "GET" and path == "/bills" and (params or {}).get("reference_number") == "MUNIM-DUP-S0":
            return httpx.Response(200, json={"bills": [{"bill_id": "zb-existing", "reference_number": "MUNIM-DUP-S0"}]})
        if method == "POST" and path == "/bills":
            assert json_body["status"] == "draft"
            assert json_body["reference_number"] == "MUNIM-G1-S1"
            assert json_body["tags"] == [{"tag_id": "tag-prj", "tag_option_id": "opt-101"}]
            assert json_body["line_items"][0]["tags"] == [{"tag_id": "tag-prj", "tag_option_id": "opt-101"}]
            assert json_body["custom_fields"] == [{"api_name": "cf_project_code", "value": "PRJ-101"}]
            return httpx.Response(200, json={"bill": {"bill_id": "zb-new", "status": "draft"}})
        if method == "GET" and path == "/invoices":
            return httpx.Response(
                200,
                json={
                    "invoices": [
                        {"invoice_id": "i1", "status": "sent", "total": 120000, "tags": [{"tag_option_id": "opt-101"}]},
                        {"invoice_id": "i2", "status": "void", "total": 99999, "tags": [{"tag_option_id": "opt-101"}]},
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        if method == "GET" and path == "/bills":
            return httpx.Response(
                200,
                json={
                    "bills": [
                        {"bill_id": "b1", "status": "draft", "total": 45000, "custom_field_hash": {"cf_project_code": "PRJ-101"}},
                        {"bill_id": "b2", "status": "void", "total": 5000, "tags": [{"tag_option_id": "opt-101"}]},
                    ],
                    "page_context": {"has_more_page": False},
                },
            )
        raise AssertionError(f"Unexpected Zoho call: {method} {path} params={params}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_request),
    ):
        bridge = await zoho.ensure_project_zoho_bridge(sb, pid)
        assert bridge["zoho_tag_id"] == "tag-prj"
        assert bridge["zoho_tag_option_id"] == "opt-101"

        bill_res = await zoho.create_draft_bill(
            supplier_name="Bikaner Timber",
            invoice_no="INV-77",
            bill_date="2026-11-16",
            line_items=[{"description": "Teak Ply", "rate": 45000, "quantity": 1}],
            vendor_id="zv-1",
            reference_number="MUNIM-G1-S1",
            project_id=pid,
            sb=sb,
        )
        assert bill_res["bill"]["bill_id"] == "zb-new"
        assert bill_res["bill"]["status"] == "draft"

        dup_res = await zoho.create_draft_bill(
            supplier_name="Bikaner Timber",
            invoice_no="INV-77",
            bill_date="2026-11-16",
            line_items=[{"description": "Teak Ply", "rate": 45000, "quantity": 1}],
            vendor_id="zv-1",
            reference_number="MUNIM-DUP-S0",
            project_id=pid,
            sb=sb,
        )
        assert dup_res["deduped"] is True
        assert dup_res["bill"]["bill_id"] == "zb-existing"

        sync_res = await zoho.sync_project_financial_caches(sb)
        assert sync_res["synced"] == 1

    prj_after = sb.store["projects"][0]
    assert prj_after["zoho_tag_id"] == "tag-prj"
    assert prj_after["zoho_tag_option_id"] == "opt-101"
    assert prj_after["zoho_invoiced_cache"] == 120000.0
    assert prj_after["zoho_bills_cache"] == 45000.0


def test_pass1_sheets_backup_v1_tabs_and_error_resilience():
    from app.services import sheets_backup

    assert "item_parts" not in sheets_backup.BACKUP_TABS
    assert "cash_ledger" not in sheets_backup.BACKUP_TABS
    assert "followups" not in sheets_backup.BACKUP_TABS
    for required_tab in (
        "customers",
        "vendors",
        "people",
        "payment_accounts",
        "projects",
        "project_items",
        "item_materials",
        "stage_events",
        "agent_goals",
        "v_project_pnl",
        "v_whos_doing_what",
        "v_master_purchase_list",
    ):
        assert required_tab in sheets_backup.BACKUP_TABS

    class FailingTabSupabase(FakeSupabase):
        def table(self, name: str):
            if name == "v_master_purchase_list":
                raise RuntimeError("view timeout")
            return super().table(name)

    sb = FailingTabSupabase({"customers": [{"id": "c1", "full_name": "Praveen"}]})
    fake_settings = Settings(backup_sheet_id="sheet-123")
    mock_service = MagicMock()
    mock_sheets_api = mock_service.spreadsheets.return_value
    mock_sheets_api.get.return_value.execute.return_value = {"sheets": []}
    with (
        patch("app.services.sheets_backup.get_settings", return_value=fake_settings),
        patch("app.services.sheets_backup.google.auth.default", return_value=(MagicMock(), "proj")),
        patch("app.services.sheets_backup.build", return_value=mock_service),
    ):
        out = sheets_backup.export_readonly_sheet_snapshot(sb)

    assert out["exported"] is True
    assert out["row_counts"]["customers"] == 1
    assert out["row_counts"]["v_master_purchase_list"] == -1
    assert out["row_counts"]["projects"] == 0


def test_pass1_customer_dedup_on_repeat_lead():
    from app.services import step_executors

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
    from pathlib import Path
    from app.services import step_executors

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


@pytest.mark.asyncio
async def test_pass1_telegram_stale_claim_recovery_and_multi_voice_burst():
    from datetime import datetime, timedelta, timezone
    from app.routers import telegram as tg_router

    now_utc = datetime.now(timezone.utc)
    sb = FakeSupabase(
        {
            "people": [
                {
                    "id": "p-owner",
                    "full_name": "Shreyansh",
                    "telegram_user_id": 1001,
                    "is_active": True,
                }
            ],
            "telegram_inbox_buffer": [
                {
                    "id": "buf-stale",
                    "telegram_update_id": 101,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "production",
                    "status": "claimed",
                    "claimed_at": (now_utc - timedelta(seconds=180)).isoformat(),
                    "created_at": (now_utc - timedelta(seconds=200)).isoformat(),
                    "raw_payload": {
                        "message": {
                            "message_id": 11,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "voice": {"file_id": "voice-1"},
                        }
                    },
                },
                {
                    "id": "buf-fresh",
                    "telegram_update_id": 102,
                    "chat_id": -9001,
                    "user_id": 1001,
                    "domain_channel": "production",
                    "status": "buffered",
                    "created_at": (now_utc - timedelta(seconds=10)).isoformat(),
                    "raw_payload": {
                        "message": {
                            "message_id": 12,
                            "chat": {"id": -9001, "type": "supergroup"},
                            "from": {"id": 1001, "first_name": "Shreyansh"},
                            "voice": {"file_id": "voice-2"},
                            "caption": "Also check kitchen",
                        }
                    },
                },
            ],
            "agent_goals": [],
        }
    )

    captured: dict[str, Any] = {}

    async def fake_download(fid: str):
        return f"audio-{fid}".encode("utf-8"), "audio/ogg"

    async def fake_run_turn(sb_arg, **kwargs):
        captured.update(kwargs)
        return {"kind": "direct_reply", "heard_transcript": "Clip1 + Clip2", "reply_text": "All noted.", "goals": []}

    fake_settings = Settings(telegram_burst_debounce_seconds=2.5)
    with (
        patch("app.routers.telegram.get_supabase_admin", return_value=sb),
        patch("app.routers.telegram.get_settings", return_value=fake_settings),
        patch("app.routers.telegram.telegram_api.download_file_bytes", side_effect=fake_download),
        patch("app.routers.telegram.telegram_api.send_message", new=AsyncMock(return_value={"result": {"message_id": 99}})),
        patch("app.routers.telegram.agent_core.run_agent_turn", side_effect=fake_run_turn),
    ):
        await tg_router.process_debounced_burst(-9001, 1001, wait_locally=False)

    assert captured["audio_bytes"] == b"audio-voice-1"
    assert captured["extra_audio_files"] == [(b"audio-voice-2", "audio/ogg")]
    assert "Also check kitchen" in (captured["text"] or "")
    assert all(r["status"] == "processed" for r in sb.store["telegram_inbox_buffer"])


@pytest.mark.asyncio
async def test_four_cash_wallets_natural_shorthand_and_zoho_safe_matching():
    from app.services import zoho

    sb = FakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-galla",
                    "code": "cash_galla",
                    "display_name": "Cash Galla",
                    "account_kind": "cash_galla",
                    "aliases": ["cash", "galla", "shop cash", "nakad"],
                    "sort_order": 1,
                    "is_active": True,
                },
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis", "axis bank"],
                    "sort_order": 2,
                    "is_active": True,
                },
                {
                    "id": "acc-rgb",
                    "code": "rgb",
                    "display_name": "RGB (Gramin Bank)",
                    "account_kind": "bank",
                    "aliases": ["rgb", "rajasthan gramin bank"],
                    "sort_order": 3,
                    "is_active": True,
                },
                {
                    "id": "acc-ccrgb",
                    "code": "ccrgb",
                    "display_name": "CCRGB (OD / CC)",
                    "account_kind": "od_cc",
                    "aliases": ["ccrgb", "cc rgb", "od"],
                    "sort_order": 4,
                    "is_active": True,
                },
                {
                    "id": "acc-ansh",
                    "code": "float_ansh",
                    "display_name": "Ansh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["ansh", "ansh wallet", "ansh float", "ansh cash", "ansh ji", "ansh bhaiya"],
                    "sort_order": 10,
                    "is_active": True,
                },
                {
                    "id": "acc-kamlesh",
                    "code": "float_kamlesh",
                    "display_name": "Kamlesh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["kamlesh", "kamlesh wallet", "kamlesh float", "kamlesh cash", "kamlesh bhaiya", "kamlesh ji"],
                    "sort_order": 11,
                    "is_active": True,
                },
                {
                    "id": "acc-rohit",
                    "code": "float_rohit",
                    "display_name": "Rohit Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["rohit", "rohit wallet", "rohit float", "rohit cash", "rohit ji", "rohit bhaiya"],
                    "sort_order": 12,
                    "is_active": True,
                },
                {
                    "id": "acc-shreyansh",
                    "code": "float_shreyansh",
                    "display_name": "Shreyansh Wallet",
                    "account_kind": "supervisor_float",
                    "aliases": ["shreyansh", "shreyansh wallet", "shreyansh float", "shreyansh cash", "shreyansh ji", "owner wallet"],
                    "sort_order": 13,
                    "is_active": True,
                },
            ]
        }
    )

    # 3-tier priority natural shorthand checks
    assert agent_core._extract_payment_account_code_from_text(sb, "Ansh paid 500 for screws") == "float_ansh"
    assert agent_core._extract_payment_account_code_from_text(sb, "Rohit paid 300 cash for auto") == "float_rohit"
    assert agent_core._extract_payment_account_code_from_text(sb, "Ansh paid 500 from galla") == "cash_galla"
    assert agent_core._extract_payment_account_code_from_text(sb, "Shreyansh paid 5,000 from Axis") == "axis"
    assert agent_core._extract_payment_account_code_from_text(sb, "Praveen Mehta advance 20,000 cash") == "cash_galla"

    # Word-bounded & account-type-safe Zoho /bankaccounts sync (note CCRGB before RGB, and SBI - Shreyansh distractor)
    fake_zoho_accs = [
        {"account_id": "z-undep", "account_name": "Undeposited Funds", "account_type": "cash", "is_active": True, "balance": -6000},
        {"account_id": "z-petty", "account_name": "Petty Cash", "account_type": "cash", "is_active": True, "balance": 100},
        {"account_id": "z-galla", "account_name": "Cash Galla", "account_type": "cash", "is_active": True, "balance": 12500},
        {"account_id": "z-ccrgb", "account_name": "CCRGB", "account_type": "bank", "is_active": True, "balance": -50000},
        {"account_id": "z-rgb", "account_name": "RGB", "account_type": "bank", "is_active": True, "balance": 42000},
        {"account_id": "z-axis", "account_name": "Axis Bank", "account_type": "bank", "is_active": True, "balance": 85000},
        {"account_id": "z-sbi-shr", "account_name": "SBI - Shreyansh", "account_type": "bank", "is_active": True, "balance": 99999},
        {"account_id": "z-ansh", "account_name": "Ansh Wallet", "account_type": "cash", "is_active": True, "balance": 4500},
        {"account_id": "z-kam", "account_name": "Kamlesh Wallet", "account_type": "cash", "is_active": True, "balance": 3000},
        {"account_id": "z-roh", "account_name": "Rohit Wallet", "account_type": "cash", "is_active": True, "balance": 2000},
        {"account_id": "z-shr", "account_name": "Shreyansh Wallet", "account_type": "cash", "is_active": True, "balance": 15000},
    ]

    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
    )

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        assert method == "GET" and path == "/bankaccounts"
        return httpx.Response(200, json={"bankaccounts": fake_zoho_accs})

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        sync_out = await zoho.sync_owner_pulse_snapshot(sb)

    assert sync_out["ok"] is True
    assert sync_out["accounts_synced"] == 8
    by_code = {a["code"]: a for a in sb.store["payment_accounts"]}
    assert by_code["cash_galla"]["zoho_account_id"] == "z-galla"
    assert by_code["axis"]["zoho_account_id"] == "z-axis"
    assert by_code["rgb"]["zoho_account_id"] == "z-rgb"
    assert by_code["ccrgb"]["zoho_account_id"] == "z-ccrgb"
    assert by_code["float_ansh"]["zoho_account_id"] == "z-ansh"
    assert by_code["float_kamlesh"]["zoho_account_id"] == "z-kam"
    assert by_code["float_rohit"]["zoho_account_id"] == "z-roh"
    assert by_code["float_shreyansh"]["zoho_account_id"] == "z-shr"


@pytest.mark.asyncio
async def test_expense_categorization_salary_routing_and_zoho_coa_selection():
    from app.services import goal_primitives, zoho

    # 1. Inference & LLM normalization across all workshop categories
    assert goal_primitives.normalize_or_infer_expense_category(None, "Paid 15,000 salary to Lala Ji") == "salary_wages"
    assert goal_primitives.normalize_or_infer_expense_category("Salaries", "") == "salary_wages"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Ansh paid 650 for tempo bhada") == "transport_freight"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Rohit paid 420 for fevicol and screws") == "raw_materials"
    assert goal_primitives.normalize_or_infer_expense_category(None, "200 chai nasta workshop") == "food_tea_mess"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Shop rent 25,000 from RGB") == "rent"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Electricity bijli bill 4500") == "utilities_office"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Cutter blade sharpening 300") == "repairs_maintenance"
    assert goal_primitives.normalize_or_infer_expense_category(None, "Random misc 100") == "other_expense"

    # 2. Salaried-karigar auto-guard flips vendor_payment -> expense(salary_wages), keeps real vendors untouched
    sb = FakeSupabase(
        {
            "payment_accounts": [
                {
                    "id": "acc-axis",
                    "code": "axis",
                    "display_name": "Axis Bank",
                    "account_kind": "bank",
                    "aliases": ["axis", "axis bank"],
                    "zoho_account_id": "za-axis",
                    "is_active": True,
                }
            ],
            "people": [
                {
                    "id": "p-lala",
                    "full_name": "Lala Ji",
                    "person_type": "salaried_karigar",
                    "aliases": ["Lala"],
                    "payout_aliases": ["Ruksar Bano"],
                    "is_active": True,
                },
                {
                    "id": "p-manish",
                    "full_name": "Manish Cushioning",
                    "person_type": "contractor_mistri",
                    "aliases": ["Manish"],
                    "payout_aliases": [],
                    "is_active": True,
                },
            ],
            "vendors": [
                {
                    "id": "v-doorset",
                    "canonical_name": "Doorset",
                    "vendor_kind": "supplier",
                    "aliases": [],
                    "is_active": True,
                }
            ],
        }
    )

    karigar_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Lala Ji",
        "amount_inr": 15000,
        "payment_account_code": "axis",
        "summary_title": "Paid Lala Ji",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, karigar_slots, user_text="Paid 15,000 to Lala Ji from Axis", heard_transcript=None
    )
    assert karigar_slots["operation_kind"] == "expense"
    assert karigar_slots["expense_category"] == "salary_wages"

    proxy_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Ruksar Bano",
        "amount_inr": 8000,
        "payment_account_code": "axis",
        "summary_title": "Transfer to Ruksar Bano",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, proxy_slots, user_text="Paid 8,000 to Ruksar Bano from Axis", heard_transcript=None
    )
    assert proxy_slots["operation_kind"] == "expense"
    assert proxy_slots["expense_category"] == "salary_wages"

    vendor_slots = {
        "operation_kind": "vendor_payment",
        "party_name": "Doorset",
        "amount_inr": 12000,
        "payment_account_code": "axis",
        "summary_title": "Paid Doorset",
    }
    agent_core._backfill_financial_slots_from_text(
        sb, vendor_slots, user_text="Paid 12,000 to Doorset from Axis", heard_transcript=None
    )
    assert vendor_slots["operation_kind"] == "vendor_payment"

    # 3. Confirmation card includes human Category line
    confirm_txt = agent_core._format_confirmation_summary("expense", karigar_slots)
    assert "Category: *Salaries & Wages*" in confirm_txt

    # 4. Zoho push selects exact Chart of Accounts account_id (never blindly index 0)
    fake_coa = [
        {"account_id": "coa-it", "account_name": "IT and Internet Expenses", "account_type": "expense", "is_active": True},
        {"account_id": "coa-sal", "account_name": "Salaries and Employee Wages", "account_type": "expense", "is_active": True},
        {"account_id": "coa-trn", "account_name": "Transportation Expense", "account_type": "expense", "is_active": True},
        {"account_id": "coa-oth", "account_name": "Other Expenses", "account_type": "expense", "is_active": True},
    ]
    fake_settings = Settings(
        zoho_client_id="cid",
        zoho_client_secret="csec",
        zoho_refresh_token="rtok",
        zoho_org_id="org1",
    )
    posted_account_ids: list[str] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path == "/expenses":
            return httpx.Response(200, json={"expenses": []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": fake_coa})
        if method == "POST" and path == "/expenses":
            posted_account_ids.append(str(json_body["account_id"]))
            return httpx.Response(200, json={"expense": {"expense_id": f"ze-{len(posted_account_ids)}"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    with (
        patch("app.services.zoho.get_settings", return_value=fake_settings),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res_sal = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=15000,
            payment_account_id="acc-axis",
            reference_number="MUNIM-SAL-1",
            party_name="Lala Ji",
            notes="October salary",
            expense_category="salary_wages",
        )
        res_trn = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=650,
            payment_account_id="acc-axis",
            reference_number="MUNIM-TRN-1",
            notes="tempo bhada",
            expense_category="transport_freight",
        )
        res_oth = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=100,
            payment_account_id="acc-axis",
            reference_number="MUNIM-OTH-1",
            notes="random",
            expense_category="other_expense",
        )

    assert posted_account_ids == ["coa-sal", "coa-trn", "coa-oth"]
    assert res_sal["expense_account_name"] == "Salaries and Employee Wages"
    assert res_trn["expense_account_name"] == "Transportation Expense"
    assert res_oth["expense_account_name"] == "Other Expenses"





