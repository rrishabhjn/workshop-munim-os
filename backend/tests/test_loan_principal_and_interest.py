"""Tests for Loan Principal (Long Term Liability) & Loan Interest recording across AgentCore and Zoho Books."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.services import agent_core, zoho
from conftest import seed_goal_intelligence_sb


def _seed_loan_sb():
    sb = seed_goal_intelligence_sb()
    sb.store["payment_accounts"].extend(
        [
            {
                "id": "acc-rgb",
                "code": "rgb",
                "display_name": "RGB",
                "account_kind": "bank",
                "zoho_account_id": "za-rgb",
                "aliases": ["rgb", "rajasthan gramin bank"],
                "is_active": True,
                "sort_order": 3,
            },
            {
                "id": "acc-ccrgb",
                "code": "ccrgb",
                "display_name": "CCRGB",
                "account_kind": "od_cc",
                "zoho_account_id": "za-ccrgb",
                "aliases": ["ccrgb", "od"],
                "is_active": True,
                "sort_order": 4,
            },
        ]
    )
    sb.store["payment_accounts"][0]["zoho_account_id"] = "za-cash"
    sb.store["payment_accounts"][1]["zoho_account_id"] = "za-axis"
    return sb


def _sample_coa(extra_loans: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    base = [
        {"account_id": "za-rgb", "account_name": "RGB", "account_type": "bank", "is_active": True},
        {"account_id": "za-ccrgb", "account_name": "CCRGB", "account_type": "bank", "is_active": True},
        {"account_id": "liab-mort", "account_name": "Mortgages", "account_type": "long_term_liability", "is_active": True},
        {"account_id": "liab-const", "account_name": "Construction Loans", "account_type": "long_term_liability", "is_active": True},
        {"account_id": "liab-rgb", "account_name": "RGB Term Loan", "account_type": "long_term_liability", "is_active": True},
        {"account_id": "exp-int", "account_name": "Interest Expense", "account_type": "expense", "is_active": True},
        {"account_id": "exp-other", "account_name": "Other Expenses", "account_type": "expense", "is_active": True},
    ]
    if extra_loans:
        base.extend(extra_loans)
    return base


def _mock_hydrogen_calls(calls: list[dict[str, Any]]):
    t1 = SimpleNamespace(
        id="int_loan_1",
        steps=[
            SimpleNamespace(
                type="function_call",
                id=f"fc_{idx}",
                name="stage_financial_entry",
                arguments=args,
            )
            for idx, args in enumerate(calls)
        ],
        output_text=None,
    )
    t2 = SimpleNamespace(id="int_loan_2", steps=[], output_text="")
    fake_c = MagicMock()
    fake_c.interactions.create.side_effect = [t1, t2]
    return fake_c


@pytest.mark.asyncio
async def test_loan_principal_and_interest_push_to_zoho_and_undo():
    sb = _seed_loan_sb()
    fake_c = _mock_hydrogen_calls(
        [
            {
                "entry_kind": "expense",
                "expense_category": "loan_principal",
                "amount_inr": 35000,
                "payment_account_code": "rgb",
                "party_name": "RGB Loan",
                "notes": "RGB loan principal",
            }
        ]
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        turn = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="RGB loan principal 35,000 paid from RGB",
        )

    g = turn["goals"][0]
    assert g["status"] == "awaiting_confirmation"
    assert g["collected_slots"]["expense_category"] == "loan_principal"
    assert g["collected_slots"]["payment_account_code"] == "rgb"
    assert g["collected_slots"]["is_new_payee"] is False
    assert "Reduces Zoho Loan Liability" in g["prompt"]

    posted: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path in ("/expenses", "/banktransactions", "/journals"):
            key = path.lstrip("/")
            return httpx.Response(200, json={key: []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": _sample_coa()})
        if method == "POST" and path == "/expenses":
            posted.append((path, dict(json_body or {})))
            return httpx.Response(200, json={"expense": {"expense_id": "ze-prin-1"}})
        if method == "DELETE":
            deleted.append(path)
            return httpx.Response(200, json={"code": 0})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    live_zoho = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    with (
        patch("app.services.zoho.get_settings", return_value=live_zoho),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        exec_res = await agent_core.confirm_and_execute_goal(
            sb, goal_id=g["goal_id"], actor_person_id="p-shreyansh", actor_label="Shreyansh"
        )
        assert exec_res["status"] == "completed"
        assert "RGB Term Loan" in exec_res["receipt"]
        assert posted[0][1]["account_id"] == "liab-rgb"
        assert posted[0][1]["paid_through_account_id"] == "za-rgb"
        assert "vendor_id" not in posted[0][1]

        undo_res = await agent_core.undo_completed_goal_async(
            sb, goal_id=g["goal_id"], actor_person_id="p-shreyansh"
        )
        assert undo_res["ok"] is True
        assert "/expenses/ze-prin-1" in deleted


@pytest.mark.asyncio
async def test_loan_principal_fallbacks_to_banktransaction_and_journal_and_ccrgb_interest():
    sb = _seed_loan_sb()
    posted_bt: list[dict[str, Any]] = []
    posted_exp: list[dict[str, Any]] = []
    deleted: list[str] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path in ("/expenses", "/banktransactions", "/journals"):
            return httpx.Response(200, json={path.lstrip("/"): []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": _sample_coa()})
        if method == "POST" and path == "/expenses":
            if (json_body or {}).get("account_id") == "liab-rgb":
                return httpx.Response(400, json={"code": 1002, "message": "Liability not allowed on expense"})
            posted_exp.append(dict(json_body or {}))
            return httpx.Response(200, json={"expense": {"expense_id": "ze-int-1"}})
        if method == "POST" and path == "/banktransactions":
            posted_bt.append(dict(json_body or {}))
            return httpx.Response(200, json={"banktransaction": {"transaction_id": "zbt-prin-1"}})
        if method == "DELETE":
            deleted.append(path)
            return httpx.Response(200, json={"code": 0})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    live_zoho = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    with (
        patch("app.services.zoho.get_settings", return_value=live_zoho),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res_bt = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=35000.0,
            payment_account_id="acc-rgb",
            party_name="RGB Loan",
            reference_number="MUNIM-TEST-BT1",
            notes="RGB loan principal",
            expense_category="loan_principal",
        )
        assert res_bt["ok"] is True
        assert res_bt["zoho_type"] == "banktransaction"
        assert res_bt["zoho_id"] == "zbt-prin-1"
        assert posted_bt[0]["from_account_id"] == "za-rgb"
        assert posted_bt[0]["to_account_id"] == "liab-rgb"

        res_int = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=14200.0,
            payment_account_id="acc-ccrgb",
            party_name="CCRGB Interest",
            reference_number="MUNIM-TEST-INT1",
            notes="CCRGB interest",
            expense_category="loan_interest",
        )
        assert res_int["ok"] is True
        assert res_int["expense_account_name"] == "Interest Expense"
        assert posted_exp[0]["account_id"] == "exp-int"
        assert posted_exp[0]["paid_through_account_id"] == "za-ccrgb"
        assert "vendor_id" not in posted_exp[0]

        g_row = {
            "id": "goal-bt-undo",
            "status": "completed",
            "steps": [{"operation": "expense", "result": {"ok": True, "zoho_type": "banktransaction", "zoho_id": "zbt-prin-1"}}],
            "collected_slots": {"operation_kind": "expense", "expense_category": "loan_principal"},
        }
        sb.store["agent_goals"].append(g_row)
        u = await agent_core.undo_completed_goal_async(sb, goal_id="goal-bt-undo")
        assert u["ok"] is True
        assert "/banktransactions/zbt-prin-1" in deleted


@pytest.mark.asyncio
async def test_compound_principal_and_interest_same_or_diff_amount_stay_separate():
    sb = _seed_loan_sb()
    fake_c = _mock_hydrogen_calls(
        [
            {
                "entry_kind": "expense",
                "expense_category": "loan_principal",
                "amount_inr": 25000,
                "payment_account_code": "rgb",
                "party_name": "RGB Loan",
                "notes": "RGB loan principal",
            },
            {
                "entry_kind": "expense",
                "expense_category": "loan_interest",
                "amount_inr": 25000,
                "payment_account_code": "rgb",
                "party_name": "RGB Loan",
                "notes": "RGB loan interest",
            },
        ]
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        turn = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="RGB loan principal 25,000 aur interest 25,000 RGB se",
        )

    assert len(turn["goals"]) == 2
    cats = [g["collected_slots"]["expense_category"] for g in turn["goals"]]
    assert cats == ["loan_principal", "loan_interest"]
    assert all(g["status"] == "awaiting_confirmation" for g in turn["goals"])


@pytest.mark.asyncio
async def test_unsplit_emi_pauses_with_choices_and_missing_bank_shows_account_picker():
    sb = _seed_loan_sb()
    fake_c = _mock_hydrogen_calls(
        [
            {
                "entry_kind": "expense",
                "expense_category": "loan_principal",
                "amount_inr": 60000,
                "payment_account_code": "rgb",
                "party_name": "RGB Loan",
                "notes": "RGB loan EMI",
            }
        ]
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        turn_emi = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="RGB loan EMI 60,000",
        )

    g_emi = turn_emi["goals"][0]
    assert g_emi["status"] == "gathering_info"
    assert [c["choice_id"] for c in g_emi["choices"]] == ["emi_principal", "emi_interest"]

    patch_obj = json.loads(g_emi["choices"][0]["slot_patch_json"])
    resumed = await agent_core.apply_slot_patch_and_resume(
        sb,
        goal_id=g_emi["goal_id"],
        slot_patch=patch_obj,
        actor_person_id="p-shreyansh",
        actor_label="Shreyansh",
    )
    assert resumed["status"] == "awaiting_confirmation"
    assert resumed["collected_slots"]["expense_category"] == "loan_principal"

    fake_c2 = _mock_hydrogen_calls(
        [
            {
                "entry_kind": "expense",
                "expense_category": "loan_principal",
                "amount_inr": 35000,
                "party_name": "Loan Principal",
                "notes": "Loan principal paid",
            }
        ]
    )
    with (
        patch("app.services.agent_core.get_settings", return_value=Settings(gemini_api_key="test-key")),
        patch("app.services.agent_core._get_interactions_client", return_value=fake_c2),
        patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)),
    ):
        turn_no_bank = await agent_core.run_agent_turn(
            sb,
            owner_person_id="p-shreyansh",
            actor_label="Shreyansh",
            source_channel="telegram",
            domain_scope="munim",
            text="Loan principal 35,000 paid",
        )
    g_nb = turn_no_bank["goals"][0]
    assert g_nb["status"] == "gathering_info"
    assert g_nb["missing_slots"] == ["payment_account_code"]
    assert any(c["choice_id"] == "acc_rgb" for c in g_nb["choices"])


def test_multi_loan_coa_disambiguation_and_override():
    coa = _sample_coa(
        extra_loans=[
            {"account_id": "liab-axis", "account_name": "Axis Machinery Loan", "account_type": "long_term_liability", "is_active": True}
        ]
    )
    hit_axis, amb1 = zoho.pick_zoho_loan_liability_account(coa, hint_text="Axis machinery loan principal")
    assert hit_axis is not None and hit_axis["account_id"] == "liab-axis"
    assert amb1 == []

    hit_ov, amb2 = zoho.pick_zoho_loan_liability_account(coa, account_name="RGB Term Loan")
    assert hit_ov is not None and hit_ov["account_id"] == "liab-rgb"
    assert amb2 == []

    hit_none, amb3 = zoho.pick_zoho_loan_liability_account(coa, hint_text="loan principal paid")
    assert hit_none is None
    assert {a["account_id"] for a in amb3} == {"liab-rgb", "liab-axis"}


@pytest.mark.asyncio
async def test_loan_principal_journal_fallback_and_text_reply_resolution():
    sb = _seed_loan_sb()
    posted_jrn: list[dict[str, Any]] = []

    async def fake_zoho_req(client, method, path, *, params=None, json_body=None):
        if method == "GET" and path in ("/expenses", "/banktransactions", "/journals"):
            return httpx.Response(200, json={path.lstrip("/"): []})
        if method == "GET" and path == "/chartofaccounts":
            return httpx.Response(200, json={"chartofaccounts": _sample_coa()})
        if method == "POST" and path in ("/expenses", "/banktransactions"):
            return httpx.Response(400, json={"code": 1002, "message": "Unsupported"})
        if method == "POST" and path == "/journals":
            posted_jrn.append(dict(json_body or {}))
            return httpx.Response(200, json={"journal": {"journal_id": "zj-prin-1"}})
        raise AssertionError(f"Unexpected Zoho call: {method} {path}")

    live_zoho = Settings(zoho_client_id="c", zoho_client_secret="s", zoho_refresh_token="r", zoho_org_id="o")
    with (
        patch("app.services.zoho.get_settings", return_value=live_zoho),
        patch("app.services.zoho._zoho_request", side_effect=fake_zoho_req),
    ):
        res_j = await zoho.push_management_entry_to_zoho(
            sb,
            entry_kind="expense",
            amount_inr=35000.0,
            payment_account_id="acc-rgb",
            party_name="RGB Loan",
            reference_number="MUNIM-TEST-JRN1",
            notes="RGB loan principal",
            expense_category="loan_principal",
        )
    assert res_j["ok"] is True
    assert res_j["zoho_type"] == "journal"
    assert res_j["zoho_id"] == "zj-prin-1"
    assert posted_jrn[0]["line_items"][0] == {
        "account_id": "liab-rgb",
        "debit_or_credit": "debit",
        "amount": 35000.0,
        "description": posted_jrn[0]["notes"],
    }
    assert posted_jrn[0]["line_items"][1]["account_id"] == "za-rgb"
    assert posted_jrn[0]["line_items"][1]["debit_or_credit"] == "credit"
