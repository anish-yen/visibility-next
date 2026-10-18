"""Durable checkpointer for the human review interrupt.

The review gate pauses the graph until an owner responds, which can be hours
or days. An in-process saver would lose that pause on any restart or redeploy
- the same class of bug as the sticky `_table_missing` flag in audit_store,
where state that looked fine in a single process quietly broke across
restarts. So the production path requires Postgres and REFUSES to start
without it rather than silently degrading to memory.

Tests pass their own saver explicitly; nothing here falls back on its own.
"""

from __future__ import annotations

from app.config import get_settings

__all__ = ["CheckpointerUnavailable", "build_checkpointer", "checkpointer_status"]


class CheckpointerUnavailable(RuntimeError):
    """Raised when the durable checkpointer cannot be constructed."""


def _connection_string() -> str:
    url = (get_settings().database_url or "").strip()
    if not url:
        raise CheckpointerUnavailable(
            "DATABASE_URL is not configured. The agentic loop pauses at the human "
            "review gate, so it needs a durable Postgres checkpointer - an "
            "in-memory one would drop every pending review on restart. Set "
            "DATABASE_URL to the Supabase Postgres connection string "
            "(Project settings -> Database -> Connection string -> URI). "
            "Never commit the value."
        )
    return url


def build_checkpointer():
    """Return an AsyncPostgresSaver context manager for the configured DB.

    Caller is responsible for entering/exiting it, and for calling
    `await saver.setup()` once so LangGraph's checkpoint tables exist.
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise CheckpointerUnavailable(
            "langgraph-checkpoint-postgres is not installed. Install the pinned "
            "versions in backend/requirements.txt."
        ) from exc

    return AsyncPostgresSaver.from_conn_string(_connection_string())


def checkpointer_status() -> dict[str, object]:
    """Non-raising probe, for diagnostics. Never returns the connection string."""
    try:
        _connection_string()
    except CheckpointerUnavailable as exc:
        return {"durable": False, "reason": str(exc).split(".")[0]}
    try:
        import langgraph.checkpoint.postgres.aio  # noqa: F401
    except ImportError:
        return {"durable": False, "reason": "langgraph-checkpoint-postgres not installed"}
    return {"durable": True, "reason": "DATABASE_URL set and postgres saver importable"}
