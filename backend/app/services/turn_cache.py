"""Per-turn read-through Supabase cache to eliminate redundant identical SELECTs inside a turn."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

_CACHEABLE_TABLES = frozenset(
    {"payment_accounts", "people", "vendors", "customers", "projects"}
)


class _CachedQueryProxy:
    def __init__(self, real_q: Any, cache: "TurnSupabaseReadCache", table_name: str):
        self._q = real_q
        self._cache = cache
        self._tbl = table_name
        self._op = "select"
        self._ops: list[tuple[str, tuple[Any, ...], tuple[tuple[str, Any], ...]]] = []
        self._cacheable = table_name in _CACHEABLE_TABLES

    def _record(self, method: str, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        if method in ("insert", "update", "delete", "upsert"):
            self._op = "write"
        elif method == "select":
            self._op = "select"
        try:
            norm_args = tuple(tuple(a) if isinstance(a, list) else a for a in args)
            norm_kw = tuple(sorted(kwargs.items()))
            self._ops.append((method, norm_args, norm_kw))
        except TypeError:
            self._cacheable = False
        self._q = getattr(self._q, method)(*args, **kwargs)
        return self

    def select(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("select", *args, **kwargs)

    def insert(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("insert", *args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("update", *args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("delete", *args, **kwargs)

    def upsert(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("upsert", *args, **kwargs)

    def eq(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("eq", *args, **kwargs)

    def neq(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("neq", *args, **kwargs)

    def in_(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("in_", *args, **kwargs)

    def ilike(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("ilike", *args, **kwargs)

    def is_(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("is_", *args, **kwargs)

    def order(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("order", *args, **kwargs)

    def limit(self, *args: Any, **kwargs: Any) -> "_CachedQueryProxy":
        return self._record("limit", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._q, name)
        if name == "not_":
            self._cacheable = False
            return _CachedQueryProxy(attr, self._cache, self._tbl)
        return attr

    def execute(self) -> Any:
        if self._op != "select":
            self._cache.invalidate(self._tbl)
            return self._q.execute()
        if not self._cacheable:
            return self._q.execute()
        key = (self._tbl, tuple(self._ops))
        cached = self._cache.get(key)
        if cached is not None:
            return SimpleNamespace(data=[dict(r) for r in cached])
        res = self._q.execute()
        data = getattr(res, "data", None)
        if isinstance(data, list):
            self._cache.set(key, [dict(r) if isinstance(r, dict) else r for r in data])
        return res


class TurnSupabaseReadCache:
    """Thread-safe per-turn wrapper sharing identical read results and invalidating on write."""

    def __init__(self, real_sb: Any):
        self._sb = real_sb
        self._lock = threading.Lock()
        self.select_cache: dict[tuple[Any, ...], list[Any]] = {}

    def get(self, key: tuple[Any, ...]) -> list[Any] | None:
        with self._lock:
            return self.select_cache.get(key)

    def set(self, key: tuple[Any, ...], rows: list[Any]) -> None:
        with self._lock:
            self.select_cache[key] = rows

    def invalidate(self, table_name: str) -> None:
        with self._lock:
            for k in [k for k in self.select_cache if k[0] == table_name]:
                self.select_cache.pop(k, None)

    def table(self, name: str) -> _CachedQueryProxy:
        return _CachedQueryProxy(self._sb.table(name), self, name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sb, name)


def wrap_turn_read_cache(sb: Any) -> Any:
    """Wrap live Supabase clients in a per-turn read cache; pass FakeSupabase through."""
    if sb is None or hasattr(sb, "store") or isinstance(sb, TurnSupabaseReadCache):
        return sb
    return TurnSupabaseReadCache(sb)
