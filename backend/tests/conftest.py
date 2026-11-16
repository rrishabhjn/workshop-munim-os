"""Shared in-memory Supabase test doubles and seed helpers for backend pytest suites."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeQuery:
    """In-memory query builder supporting select/insert/update/delete/eq/in_/ilike/order/limit."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._op = "select"
        self._payload: Any = None
        self._filters: list[Any] = []
        self._limit: int | None = None
        self._order_col: str | None = None
        self._order_desc: bool = False

    def select(self, *_args: Any, **_kwargs: Any) -> "FakeQuery":
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


class FakeTable:
    """In-memory Supabase table double delegating query builders via `_q()`."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def _q(self) -> FakeQuery:
        return FakeQuery(self.rows)

    def select(self, *_args: Any, **_kwargs: Any) -> FakeQuery:
        return self._q().select()

    def insert(self, payload: dict[str, Any] | list[dict[str, Any]]) -> FakeQuery:
        return self._q().insert(payload)

    def update(self, patch_obj: dict[str, Any]) -> FakeQuery:
        return self._q().update(patch_obj)

    def delete(self) -> FakeQuery:
        return self._q().delete()


class FakeSupabase:
    """In-memory Supabase client double backed by a `store` dict."""

    def __init__(self, initial: dict[str, list[dict[str, Any]]] | None = None):
        self.store: dict[str, list[dict[str, Any]]] = initial or {}

    def table(self, name: str) -> FakeTable:
        return FakeTable(self.store.setdefault(name, []))


def _build_seed_sb(
    *,
    third_person: dict[str, Any],
    project_items: list[dict[str, Any]],
) -> FakeSupabase:
    """Build parameterized in-memory Workshop Munim seed state."""
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
                third_person,
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
            "project_items": project_items,
            "stage_events": [],
            "agent_goals": [],
            "workshop_memory": [],
            "vendors": [],
        }
    )


def seed_goal_intelligence_sb() -> FakeSupabase:
    """Seed fixture matching the goal intelligence suite (`p-lala` + `item-kitchen`)."""
    return _build_seed_sb(
        third_person={
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
        project_items=[
            {
                "id": "item-kitchen",
                "project_id": "prj-ambure",
                "item_name": "Kitchen",
                "stage": "in_shop",
                "production_mistri_id": "p-lala",
            }
        ],
    )


def seed_phase2_sb() -> FakeSupabase:
    """Seed fixture matching the phase-2 bridge suite (`p-ramesh` + empty `project_items`)."""
    return _build_seed_sb(
        third_person={
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
        project_items=[],
    )
