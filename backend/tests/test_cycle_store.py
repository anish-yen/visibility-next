"""cycle_store: in-memory roundtrip + Supabase row mapping (fake client)."""
import asyncio
from types import SimpleNamespace

from app import audit_store
from app.cycle_store import InMemorySnapshotStore, SupabaseSnapshotStore, _row_to_snapshot


def _snap(audit_id, n, rate, lift=None, decision="continue"):
    return {
        "audit_id": audit_id,
        "cycle_number": n,
        "mention_rate": rate,
        "prompt_set": [{"id": "p1", "text": "best x for y", "intent": "best"}],
        "prompt_results": [{"id": "p1", "score": rate}],
        "citation_map": {"total_citations": 0},
        "tech_checks": [],
        "artifacts": [],
        "lift": lift,
        "decision": decision,
        "decision_reason": "",
    }


def test_memory_roundtrip():
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u1", "acme.com", [], None)
    asyncio.run(store.save(_snap(a.id, 0, 0.25)))
    asyncio.run(store.save(_snap(a.id, 1, 0.5, lift=0.25)))

    latest = asyncio.run(store.latest(a.id))
    assert latest["cycle_number"] == 1
    assert latest["lift"] == 0.25
    assert asyncio.run(store.latest("no-such-audit")) is None

    rows = asyncio.run(store.list(a.id))
    assert [r["cycle_number"] for r in rows] == [1, 0]

    c0 = asyncio.run(store.get(a.id, 0))
    assert c0["mention_rate"] == 0.25
    assert c0["prompt_set"][0]["id"] == "p1"
    assert asyncio.run(store.get(a.id, 9)) is None


class _FakeTable:
    """Minimal supabase-py table double: records inserts, serves selects."""

    def __init__(self, rows=None):
        self.inserted = []
        self._rows = list(rows or [])
        self._mode = None

    # chainable query API -------------------------------------------------
    def insert(self, row):
        self.inserted.append(dict(row))
        self._mode = "insert"
        return self

    def select(self, *_cols):
        self._mode = "select"
        self._result = list(self._rows)
        return self

    def eq(self, col, val):
        self._result = [r for r in self._result if r.get(col) == val]
        return self

    def order(self, col, desc=False):
        self._result.sort(key=lambda r: r.get(col, 0), reverse=desc)
        return self

    def limit(self, n):
        self._result = self._result[:n]
        return self

    def execute(self):
        if self._mode == "insert":
            return SimpleNamespace(data=[{"id": "row-1", **self.inserted[-1]}])
        return SimpleNamespace(data=self._result)


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def table(self, name):
        assert name == "visibility_cycles"
        return self._table


def _stored_row(audit_id, user_id, n, rate, lift=None):
    return {
        "id": f"row-{n}",
        "audit_id": audit_id,
        "user_id": user_id,
        "cycle_number": n,
        "mention_rate": str(rate),  # postgrest numeric can come back as str
        "citation_map": {},
        "prompt_results": [],
        "prompt_set": [{"id": "p1", "text": "t", "intent": "best"}],
        "tech_checks": [],
        "artifacts": [],
        "lift": str(lift) if lift is not None else None,
        "decision": "continue",
        "decision_reason": "",
        "created_at": "2026-09-25T00:00:00+00:00",
    }


def test_supabase_save_maps_user_and_prompt_set():
    a = audit_store.create_audit("u9", "acme.com", [], None)
    table = _FakeTable()
    store = SupabaseSnapshotStore(client=_FakeClient(table))
    sid = asyncio.run(store.save(_snap(a.id, 0, 0.25)))
    assert sid == "row-1"
    row = table.inserted[0]
    assert row["user_id"] == "u9"
    assert row["audit_id"] == a.id
    assert row["prompt_set"][0]["id"] == "p1"
    assert "snapshot_id" not in row  # not a column


def test_supabase_latest_and_list_reconstruct_snapshots():
    a = audit_store.create_audit("u10", "acme.com", [], None)
    table = _FakeTable(rows=[_stored_row(a.id, "u10", 0, 0.25), _stored_row(a.id, "u10", 1, 0.5, lift=0.25)])
    store = SupabaseSnapshotStore(client=_FakeClient(table))

    latest = asyncio.run(store.latest(a.id))
    assert latest["cycle_number"] == 1
    assert latest["mention_rate"] == 0.5
    assert latest["lift"] == 0.25
    assert latest["snapshot_id"] == "row-1"
    assert isinstance(latest["prompt_set"], list)

    rows = asyncio.run(store.list(a.id))
    assert [r["cycle_number"] for r in rows] == [1, 0]

    c0 = asyncio.run(store.get(a.id, 0))
    assert c0["mention_rate"] == 0.25
    assert asyncio.run(store.get(a.id, 42)) is None


def test_row_to_snapshot_defaults_for_null_jsonb():
    snap = _row_to_snapshot({"id": "x", "audit_id": "a", "cycle_number": 0})
    assert snap["prompt_set"] == []
    assert snap["citation_map"] == {}
    assert snap["mention_rate"] is None
