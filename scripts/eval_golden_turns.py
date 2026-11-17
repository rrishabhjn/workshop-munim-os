"""Dry-run Golden Turn Replay Evaluator against live `gemini-4-hydrogen` (zero DB/Zoho writes)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pathlib
import sys
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "tests"))

from app.config import Settings  # noqa: E402
from app.services import agent_core  # noqa: E402
from conftest import FakeSupabase  # noqa: E402


def _build_eval_sb() -> FakeSupabase:
    now_iso = datetime.now(timezone.utc).isoformat()
    return FakeSupabase(
        {
            "payment_accounts": [
                {"id": "acc-cash", "code": "cash_galla", "display_name": "Cash Galla", "account_kind": "cash_galla", "aliases": ["cash", "galla"], "is_active": True, "sort_order": 1},
                {"id": "acc-axis", "code": "axis", "display_name": "Axis Bank", "account_kind": "bank", "aliases": ["axis", "axis bank"], "is_active": True, "sort_order": 2},
                {"id": "acc-rgb", "code": "rgb", "display_name": "RGB Bank", "account_kind": "bank", "aliases": ["rgb", "gramin bank"], "is_active": True, "sort_order": 3},
            ],
            "people": [
                {"id": "p-shr", "full_name": "Shreyansh", "person_type": "operator", "role_tags": ["owner"], "is_active": True},
                {"id": "p-raj", "full_name": "Rajesh Ji", "person_type": "salaried_karigar", "role_tags": ["karigar"], "aliases": ["Rajesh"], "is_active": True},
                {"id": "p-brij", "full_name": "Brijesh Ji", "person_type": "salaried_karigar", "role_tags": ["karigar"], "aliases": ["Brijesh"], "is_active": True},
            ],
            "vendors": [
                {"id": "v-suresh", "canonical_name": "Suresh Ji", "vendor_kind": "contractor", "specialty": "Painter", "zoho_contact_id": "zv-suresh", "aliases": [], "is_active": True},
                {"id": "v-balaji", "canonical_name": "Balaji Ply", "vendor_kind": "supplier", "specialty": "Plywood", "zoho_contact_id": "zv-balaji", "aliases": [], "is_active": True},
            ],
            "customers": [
                {"id": "c-sam", "full_name": "Samiksha Mam", "area_landmark": "Ajmer", "zoho_contact_id": "zc-sam", "aliases": ["Samiksha"], "is_active": True},
                {"id": "c-arjun", "full_name": "Arjun", "area_landmark": "Jaipur", "zoho_contact_id": "zc-arjun", "aliases": [], "is_active": True},
            ],
            "projects": [
                {
                    "id": "prj-sam",
                    "project_code": "PRJ-OCT-007",
                    "customer_id": "c-sam",
                    "customer_name": "Samiksha Mam",
                    "site_area_landmark": "Ajmer",
                    "rough_scope": "Full House Doors & Wardrobes",
                    "project_type": "customer",
                    "status": "active",
                    "pipeline_stage": "execution",
                    "material_stage": "ordered_pending",
                    "payment_stage": "advance_in",
                    "quoted_total": 450000.0,
                    "paid_cache_inr": 100000.0,
                    "updated_at": now_iso,
                },
                {
                    "id": "prj-arjun",
                    "project_code": "PRJ-261117-014004",
                    "customer_id": "c-arjun",
                    "customer_name": "Arjun (Jaipur)",
                    "site_area_landmark": "Jaipur",
                    "rough_scope": "3 Rooms Beds & Almirahs",
                    "project_type": "customer",
                    "status": "active",
                    "pipeline_stage": "execution",
                    "material_stage": "check_stock",
                    "payment_stage": "advance_pending",
                    "quoted_total": 0.0,
                    "paid_cache_inr": 0.0,
                    "updated_at": now_iso,
                },
            ],
            "project_items": [
                {"id": "it-bed", "project_id": "prj-arjun", "item_name": "Mom Room (1) - 10ft Bed", "stage": "in_shop"},
            ],
            "item_materials": [],
            "stage_events": [],
            "agent_goals": [],
            "workshop_memory": [],
        }
    )


GOLDEN_CASES = [
    ("munim", "50,000 received in rgb bank via Samiksha", {"count": 1, "op": "customer_receipt", "amount": 50000, "acc": "rgb"}),
    ("munim", "1,05,000 cash sales to chiranji lal for door and laminate sales", {"count": 1, "op": "customer_receipt", "amount": 105000, "acc": "cash_galla"}),
    ("munim", "1000 each paid to rajesh and brijesh in cash\n\nSalary advance", {"count": 2, "op": "expense", "cat": "salary_wages", "amount": 1000, "acc": "cash_galla"}),
    ("munim", "5000 cash paid to suresh ji painter", {"count": 1, "op": "expense", "cat": "contractor_labor", "amount": 5000, "acc": "cash_galla"}),
    ("munim", "Ramesh Verma owes us 25,000 for wardrobe repair", {"count": 1, "op": "customer_draft_invoice", "amount": 25000}),
    ("munim", "Balaji Ply ka 18,500 udhaar bill chadha do (we owe them, unpaid)", {"count": 1, "op": "supplier_draft_bill", "amount": 18500}),
    ("munim", "Settled Balaji Ply pending bill 18,500 from Axis", {"count": 1, "op": "vendor_payment", "amount": 18500, "acc": "axis"}),
    ("munim", "300 rupaye chai nasta cash se", {"count": 1, "op": "expense", "cat": "food_tea_mess", "amount": 300, "acc": "cash_galla"}),
    ("munim", "my last 5 transactions?", {"count": 0, "kind": "direct_answer"}),
    ("production", "kaunse orders mein material chahiye?", {"count": 0, "kind": "direct_answer"}),
    ("production", "For Arjun Jaipur Mom room 10ft Bed, add 1 mattress and 1 ply as required material", {"count": 1, "op": "item_material_upsert"}),
    ("production", "Samiksha order: Kitchen packed ready ho gaya", {"count": 1, "op": "item_upsert_or_move"}),
]


async def main() -> int:
    passed = 0
    for idx, (scope, prompt, expect) in enumerate(GOLDEN_CASES, 1):
        sb = _build_eval_sb()
        with patch("app.services.zoho.get_settings", return_value=Settings(_env_file=None)):
            res = await agent_core.run_agent_turn(
                sb,
                owner_person_id="p-shr",
                actor_label="Shreyansh",
                source_channel="telegram",
                domain_scope=scope,  # type: ignore[arg-type]
                text=prompt,
            )
        goals = res.get("goals") or []
        ok = len(goals) == expect["count"]
        if ok and expect["count"] == 0 and "kind" in expect:
            ok = res.get("kind") in (expect["kind"], "direct_reply")
        if ok and expect["count"] >= 1:
            slots = goals[0].get("collected_slots") or {}
            if "op" in expect:
                ok = ok and slots.get("operation_kind") == expect["op"]
            if "cat" in expect:
                ok = ok and slots.get("expense_category") == expect["cat"]
            if "amount" in expect:
                ok = ok and abs(float(slots.get("amount_inr") or 0) - expect["amount"]) < 0.01
            if "acc" in expect:
                ok = ok and slots.get("payment_account_code") == expect["acc"]

        status_icon = "PASS" if ok else "FAIL"
        passed += int(ok)
        detail = (
            f"kind={res.get('kind')}"
            if not goals
            else f"n={len(goals)} op={(goals[0].get('collected_slots') or {}).get('operation_kind')} cat={(goals[0].get('collected_slots') or {}).get('expense_category')} amt={(goals[0].get('collected_slots') or {}).get('amount_inr')} acc={(goals[0].get('collected_slots') or {}).get('payment_account_code')} party={(goals[0].get('collected_slots') or {}).get('party_name')}"
        )
        print(f"[{status_icon}] #{idx:02d} ({scope}): {prompt.splitlines()[0][:55]!r} -> {detail}")
    print(f"\nGolden Eval Score: {passed}/{len(GOLDEN_CASES)}")
    return 0 if passed == len(GOLDEN_CASES) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
