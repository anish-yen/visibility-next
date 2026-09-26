"""Cycle snapshot persistence for the agentic visibility loop.

Two backends behind the loop_runner.SnapshotStore duck-type:

- InMemorySnapshotStore: zero-setup default, dies on restart. Used when
  Supabase env is not configured (local dev, tests).
- SupabaseSnapshotStore: persists to the visibility_cycles table (migration
  backend/supabase/migrations/visibility_cycles.sql). Writes use the service
  role (bypasses RLS); users read their own rows per the table policy.

get_cycle_store() picks from env, so endpoints and the runner never care
which backend is active.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from app import audit_store
from app.config import get_settings

TABLE = "visibility_cycles"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """Map a visibility_cycles row back to the snapshot dict run_cycle uses."""
    snap = {
        "snapshot_id": row.get("id"),
        "audit_id": row.get("audit_id"),
        "cycle_number": row.get("cycle_number"),
        "mention_rate": row.get("mention_rate"),
        "citation_map": row.get("citation_map") or {},
        "prompt_results": row.get("prompt_results") or [],
        "prompt_set": row.get("prompt_set") or [],
        "tech_checks": row.get("tech_checks") or [],
        "artifacts": row.get("artifacts") or [],
        "lift": row.get("lift"),
        "decision": row.get("decision"),
        "decision_reason": row.get("decision_reason"),
        "created_at": row.get("created_at"),
    }
    # PostgREST can return numeric columns as strings depending on client version
    if snap["mention_rate"] is not None:
        snap["mention_rate"] = float(snap["mention_rate"])
    if snap["lift"] is not None:
        snap["lift"] = float(snap["lift"])
    return snap


def _snapshot_to_row(snapshot: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    return {
        "audit_id": snapshot["audit_id"],
        "user_id": user_id,
        "cycle_number": snapshot["cycle_number"],
        "mention_rate": snapshot.get("mention_rate"),
        "citation_map": snapshot.get("citation_map") or {},
        "prompt_results": snapshot.get("prompt_results") or [],
        "prompt_set": snapshot.get("prompt_set") or [],
        "tech_checks": snapshot.get("tech_checks") or [],
        "artifacts": snapshot.get("artifacts") or [],
        "lift": snapshot.get("lift"),
        "decision": snapshot.get("decision"),
        "decision_reason": snapshot.get("decision_reason"),
    }


class InMemorySnapshotStore:
    """Process-local store. Same shape as the Supabase rows."""

    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {}

    async def save(self, snapshot: dict[str, Any]) -> str:
        row = dict(snapshot)
        row.setdefault("snapshot_id", str(uuid.uuid4()))
        row.setdefault("created_at", _iso())
        rows = self._rows.setdefault(snapshot["audit_id"], [])
        rows[:] = [r for r in rows if r.get("cycle_number") != row.get("cycle_number")]
        rows.append(row)
        return row["snapshot_id"]

    async def latest(self, audit_id: str) -> dict[str, Any] | None:
        rows = self._rows.get(audit_id, [])
        if not rows:
            return None
        return max(rows, key=lambda r: r.get("cycle_number", -1))

    async def list(self, audit_id: str) -> list[dict[str, Any]]:
        rows = self._rows.get(audit_id, [])
        return sorted(rows, key=lambda r: r.get("cycle_number", -1), reverse=True)

    async def get(self, audit_id: str, cycle_number: int) -> dict[str, Any] | None:
        for row in self._rows.get(audit_id, []):
            if row.get("cycle_number") == cycle_number:
                return row
        return None


class SupabaseSnapshotStore:
    """visibility_cycles table via the service-role client (sync, so calls
    are pushed off the event loop with asyncio.to_thread)."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _db(self) -> Any:
        if self._client is None:
            from app.supabase_client import get_supabase_admin

            self._client = get_supabase_admin()
        return self._client

    async def save(self, snapshot: dict[str, Any]) -> str:
        audit = audit_store.get(snapshot["audit_id"])
        if audit is None:
            raise RuntimeError(f"audit {snapshot['audit_id']} not found for snapshot save")
        row = _snapshot_to_row(snapshot, user_id=audit.user_id)

        def _insert() -> str:
            res = self._db().table(TABLE).insert(row).execute()
            data = res.data or []
            if not data:
                raise RuntimeError("visibility_cycles insert returned no row")
            return data[0]["id"]

        return await asyncio.to_thread(_insert)

    async def latest(self, audit_id: str) -> dict[str, Any] | None:
        def _select() -> dict[str, Any] | None:
            res = (
                self._db()
                .table(TABLE)
                .select("*")
                .eq("audit_id", audit_id)
                .order("cycle_number", desc=True)
                .limit(1)
                .execute()
            )
            return (res.data or [None])[0]

        row = await asyncio.to_thread(_select)
        return _row_to_snapshot(row) if row else None

    async def list(self, audit_id: str) -> list[dict[str, Any]]:
        def _select() -> list[dict[str, Any]]:
            res = (
                self._db()
                .table(TABLE)
                .select("*")
                .eq("audit_id", audit_id)
                .order("cycle_number", desc=True)
                .execute()
            )
            return res.data or []

        return [_row_to_snapshot(r) for r in await asyncio.to_thread(_select)]

    async def get(self, audit_id: str, cycle_number: int) -> dict[str, Any] | None:
        def _select() -> dict[str, Any] | None:
            res = (
                self._db()
                .table(TABLE)
                .select("*")
                .eq("audit_id", audit_id)
                .eq("cycle_number", cycle_number)
                .limit(1)
                .execute()
            )
            return (res.data or [None])[0]

        row = await asyncio.to_thread(_select)
        return _row_to_snapshot(row) if row else None


_memory_store = InMemorySnapshotStore()


def get_cycle_store() -> InMemorySnapshotStore | SupabaseSnapshotStore:
    """Supabase when configured, shared in-memory store otherwise."""
    s = get_settings()
    if s.supabase_url and s.supabase_service_key:
        return SupabaseSnapshotStore()
    return _memory_store
