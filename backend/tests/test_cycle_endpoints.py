"""Cycle endpoints: auth/ownership, 202 kick-off, list + detail reads."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import audit_store
from app import cycle_store as cycle_store_mod
from app import loop_runner
from app.cycle_store import InMemorySnapshotStore
from app.routers import audits as audits_router


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user_id = request.headers.get("x-test-user", "u1")
        return await call_next(request)

    app.include_router(audits_router.router)
    return app


def _seed(store, audit_id, n=0):
    asyncio.run(store.save({
        "audit_id": audit_id, "cycle_number": n, "mention_rate": 0.25,
        "lift": None, "decision": "continue", "decision_reason": "",
        "prompt_set": [{"id": "p1", "text": "t", "intent": "best"}],
        "prompt_results": [], "citation_map": {"total_citations": 0},
        "tech_checks": [], "artifacts": [],
    }))


def test_post_cycle_kicks_off_background_run(monkeypatch):
    store = InMemorySnapshotStore()
    monkeypatch.setattr(cycle_store_mod, "get_cycle_store", lambda: store)
    monkeypatch.setattr(audits_router.cycle_store, "get_cycle_store", lambda: store)

    calls = []

    async def fake_run_cycle(audit_id, snapshot_store, **kw):
        calls.append((audit_id, snapshot_store))
        return {"cycle_number": 0}

    monkeypatch.setattr(audits_router.loop_runner, "run_cycle", fake_run_cycle)
    assert audits_router.loop_runner is loop_runner  # patches the real entrypoint

    a = audit_store.create_audit("u1", "acme.com", [], None)
    client = TestClient(_app())
    r = client.post(f"/audits/{a.id}/cycle")
    assert r.status_code == 202
    assert r.json() == {"status": "running", "audit_id": a.id}
    # TestClient waits for background tasks before returning
    assert calls == [(a.id, store)]


def test_list_and_get_cycles(monkeypatch):
    store = InMemorySnapshotStore()
    monkeypatch.setattr(audits_router.cycle_store, "get_cycle_store", lambda: store)
    a = audit_store.create_audit("u1", "acme.com", [], None)
    _seed(store, a.id, n=0)
    _seed(store, a.id, n=1)

    client = TestClient(_app())
    r = client.get(f"/audits/{a.id}/cycles")
    assert r.status_code == 200
    body = r.json()
    assert [c["cycle_number"] for c in body] == [1, 0]
    assert set(body[0]) >= {"snapshot_id", "cycle_number", "mention_rate",
                            "lift", "decision", "decision_reason", "created_at"}

    r = client.get(f"/audits/{a.id}/cycles/0")
    assert r.status_code == 200
    assert r.json()["prompt_set"][0]["id"] == "p1"

    assert client.get(f"/audits/{a.id}/cycles/99").status_code == 404


def test_ownership_enforced(monkeypatch):
    store = InMemorySnapshotStore()
    monkeypatch.setattr(audits_router.cycle_store, "get_cycle_store", lambda: store)
    a = audit_store.create_audit("u2", "acme.com", [], None)
    client = TestClient(_app())
    # default test user is u1; audit belongs to u2
    assert client.post(f"/audits/{a.id}/cycle").status_code == 404
    assert client.get(f"/audits/{a.id}/cycles").status_code == 404
    assert client.get(f"/audits/{a.id}/cycles/0").status_code == 404
    # and a totally unknown id 404s too
    assert client.post("/audits/nope/cycle").status_code == 404
