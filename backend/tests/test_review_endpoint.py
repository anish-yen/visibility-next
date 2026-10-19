"""Review-gate endpoints: owner scoping, resume plumbing, failure modes.

Two layers:
  - router tests fake the graph helpers, to exercise auth, conflict and
    unavailable-checkpointer handling;
  - helper tests drive the REAL graph through a MemorySaver, to exercise the
    actual pause/resume path the endpoints call.

As with test_loop_graph.py, these are PLUMBING tests. No Gemini or other LLM
call is made, no Postgres is contacted, and nothing here measures answer or
prompt quality.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import audit_store
from app.routers import review as review_router

pytest.importorskip("langgraph", reason="langgraph not installed in this environment")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.graph import loop_graph  # noqa: E402
from app.graph import nodes as graph_nodes  # noqa: E402
from app.graph.checkpointer import CheckpointerUnavailable  # noqa: E402

from tests.test_loop_graph import PROMPTS, _patch_graph  # noqa: E402

OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _app(user_id=OWNER, email="owner@example.com"):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user_id = request.headers.get("x-test-user", user_id)
        request.state.user = {"email": request.headers.get("x-test-email", email)}
        return await call_next(request)

    app.include_router(review_router.router)
    return app


# --- router behaviour (graph helpers faked) -----------------------------

def test_get_review_reports_nothing_awaiting(monkeypatch):
    async def none_pending(audit_id, **kw):
        return None

    monkeypatch.setattr(loop_graph, "get_pending_review", none_pending)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    body = TestClient(_app()).get(f"/audits/{audit.id}/review").json()

    assert body["awaiting_review"] is False
    assert body["payload"] is None


def test_get_review_surfaces_the_pending_payload(monkeypatch):
    async def pending(audit_id, **kw):
        return {
            "awaiting": "artifact_review",
            "payload": {"kind": "artifact_review", "artifacts": [{"artifact_type": "faq_block"}]},
            "next_nodes": ["human_review"],
        }

    monkeypatch.setattr(loop_graph, "get_pending_review", pending)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    body = TestClient(_app()).get(f"/audits/{audit.id}/review").json()

    assert body["awaiting_review"] is True
    assert body["payload"]["artifacts"][0]["artifact_type"] == "faq_block"
    assert body["next_nodes"] == ["human_review"]


def test_review_endpoints_are_owner_scoped(monkeypatch):
    async def pending(audit_id, **kw):
        return {"payload": {}, "next_nodes": []}

    monkeypatch.setattr(loop_graph, "get_pending_review", pending)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)
    client = TestClient(_app())

    headers = {"x-test-user": OTHER}
    assert client.get(f"/audits/{audit.id}/review", headers=headers).status_code == 404
    assert client.post(
        f"/audits/{audit.id}/review", json={"decision": "approve"}, headers=headers
    ).status_code == 404


def test_post_review_conflicts_when_not_paused(monkeypatch):
    async def none_pending(audit_id, **kw):
        return None

    monkeypatch.setattr(loop_graph, "get_pending_review", none_pending)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    r = TestClient(_app()).post(f"/audits/{audit.id}/review", json={"decision": "approve"})

    assert r.status_code == 409
    assert "not waiting for review" in r.json()["detail"]


def test_unconfigured_checkpointer_returns_503_not_500(monkeypatch):
    async def unavailable(audit_id, **kw):
        raise CheckpointerUnavailable("DATABASE_URL is not configured.")

    monkeypatch.setattr(loop_graph, "get_pending_review", unavailable)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    r = TestClient(_app()).get(f"/audits/{audit.id}/review")

    assert r.status_code == 503
    assert "DATABASE_URL" in r.json()["detail"]


def test_reviewer_is_taken_from_the_token_not_the_request_body(monkeypatch):
    """An approval must not be attributable to someone the caller names."""
    captured = {}

    async def pending(audit_id, **kw):
        return {"payload": {}, "next_nodes": ["human_review"]}

    async def resume(audit_id, decision, **kw):
        captured.update(decision)
        return {"decision": "stop", "decision_reason": "owner rejected the drafts"}

    monkeypatch.setattr(loop_graph, "get_pending_review", pending)
    monkeypatch.setattr(loop_graph, "resume_graph_cycle", resume)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    TestClient(_app(email="real@example.com")).post(
        f"/audits/{audit.id}/review",
        json={"decision": "reject", "reviewer": "someone-else@evil.com", "notes": "no"},
    )

    assert captured["reviewer"] == "real@example.com"


def test_published_flag_is_ignored_unless_approving(monkeypatch):
    """published only means anything with approve; a reject that claims
    publication must not be recorded as published."""
    captured = {}

    async def pending(audit_id, **kw):
        return {"payload": {}, "next_nodes": ["human_review"]}

    async def resume(audit_id, decision, **kw):
        captured.update(decision)
        return {"decision": "stop", "decision_reason": "x"}

    monkeypatch.setattr(loop_graph, "get_pending_review", pending)
    monkeypatch.setattr(loop_graph, "resume_graph_cycle", resume)
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    TestClient(_app()).post(
        f"/audits/{audit.id}/review", json={"decision": "reject", "published": True}
    )

    assert captured["published"] is False


def test_rejects_an_unknown_decision_value(monkeypatch):
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)
    r = TestClient(_app()).post(
        f"/audits/{audit.id}/review", json={"decision": "ship-it-now"}
    )
    assert r.status_code == 422


# --- real graph through the helpers the endpoints call ------------------

def test_helpers_read_and_resume_a_real_paused_graph(monkeypatch):
    """End-to-end through get_pending_review + resume_graph_cycle with a real
    compiled graph and a MemorySaver, which is what the endpoints do once a
    checkpointer exists."""
    _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    saver = MemorySaver()
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)

    graph = loop_graph.build_graph(saver)
    first = asyncio.run(
        graph.ainvoke(loop_graph.initial_state(audit.id),
                      config={"configurable": {"thread_id": audit.id}})
    )
    assert "__interrupt__" in first

    pending = asyncio.run(loop_graph.get_pending_review(audit.id, checkpointer=saver))
    assert pending is not None
    assert pending["payload"]["kind"] == "artifact_review"
    assert "human_review" in pending["next_nodes"]

    out = asyncio.run(loop_graph.resume_graph_cycle(
        audit.id, {"decision": "reject", "reviewer": "owner@example.com"}, checkpointer=saver
    ))
    assert out["decision"] == "stop"
    assert "rejected" in out["decision_reason"]

    # once resumed and ended, nothing is awaiting review any more
    assert asyncio.run(loop_graph.get_pending_review(audit.id, checkpointer=saver)) is None


def test_get_pending_review_is_none_for_an_unknown_thread():
    assert asyncio.run(
        loop_graph.get_pending_review("no-such-audit", checkpointer=MemorySaver())
    ) is None


def test_resume_with_approve_and_published_triggers_the_retest(monkeypatch):
    calls = _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    saver = MemorySaver()
    audit = audit_store.create_audit(OWNER, "acme.com", [], None)
    graph = loop_graph.build_graph(saver)
    asyncio.run(graph.ainvoke(loop_graph.initial_state(audit.id),
                              config={"configurable": {"thread_id": audit.id}}))
    engine_calls_before = sum(1 for c in calls if c[0] == "engine")

    out = asyncio.run(loop_graph.resume_graph_cycle(
        audit.id, {"decision": "approve", "published": True, "reviewer": "o@e.com"},
        checkpointer=saver,
    ))

    assert out["retest"]["measured_on_frozen_prompt_set"] is True
    assert sum(1 for c in calls if c[0] == "engine") > engine_calls_before
