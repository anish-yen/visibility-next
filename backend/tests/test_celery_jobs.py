"""Regression tests for background audit execution (Celery + Redis).

No Gemini calls: audit_pipeline.run_audit is always replaced with a fake.
The integration test uses a real redis-server (via redislite, skipped if
unavailable) and a real Celery worker thread.
"""
import asyncio
import socket
import time

import pytest

from app import audit_pipeline, job_dispatch


class _S:
    def __init__(self, url):
        self.redis_url = url


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _drain():
    async def go():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    return go


def test_no_redis_runs_inline(monkeypatch):
    ran = []

    async def fake(audit_id):
        ran.append(audit_id)

    monkeypatch.setattr(audit_pipeline, "run_audit", fake)
    monkeypatch.setattr(job_dispatch, "get_settings", lambda: _S(""))

    async def go():
        assert job_dispatch.enqueue_audit("a1") == "inline"
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert ran == ["a1"]


def test_redis_url_enqueues_to_celery(monkeypatch):
    sent = []
    from app import tasks

    monkeypatch.setattr(job_dispatch, "get_settings", lambda: _S("redis://x"))
    monkeypatch.setattr(tasks.run_audit_task, "delay", lambda aid: sent.append(aid))
    assert job_dispatch.enqueue_audit("a2") == "celery"
    assert sent == ["a2"]


def test_enqueue_failure_falls_back_inline(monkeypatch):
    from app import tasks

    ran = []

    async def fake(audit_id):
        ran.append(audit_id)

    def boom(aid):
        raise ConnectionError("redis down")

    monkeypatch.setattr(audit_pipeline, "run_audit", fake)
    monkeypatch.setattr(job_dispatch, "get_settings", lambda: _S("redis://x"))
    monkeypatch.setattr(tasks.run_audit_task, "delay", boom)

    async def go():
        assert job_dispatch.enqueue_audit("a3") == "inline"
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert ran == ["a3"]


def test_task_runs_pipeline(monkeypatch):
    from app import tasks

    ran = []

    async def fake(audit_id):
        ran.append(audit_id)

    monkeypatch.setattr(audit_pipeline, "run_audit", fake)
    assert tasks.run_audit_task.apply(args=["a4"]).get() == "a4"
    assert ran == ["a4"]


def test_task_is_not_auto_retried():
    from app import tasks

    assert not getattr(tasks.run_audit_task, "autoretry_for", ())


def test_real_redis_worker_end_to_end(monkeypatch):
    redislite = pytest.importorskip("redislite")
    from celery.contrib.testing.worker import start_worker

    port = _free_port()
    r = redislite.Redis(serverconfig={"port": str(port)})
    url = f"redis://127.0.0.1:{port}/0"
    try:
        assert r.ping()
        done = []

        async def fake(audit_id):
            done.append(audit_id)

        monkeypatch.setattr(audit_pipeline, "run_audit", fake)
        from app import tasks
        from app.celery_app import celery_app

        celery_app.conf.update(broker_url=url, result_backend=url)
        celery_app._pool = None
        monkeypatch.setattr(job_dispatch, "get_settings", lambda: _S(url))
        with start_worker(celery_app, perform_ping_check=False, pool="solo", loglevel="WARNING"):
            assert job_dispatch.enqueue_audit("e2e-1") == "celery"
            tasks.run_audit_task.delay("e2e-2")
            for _ in range(200):
                if len(done) == 2:
                    break
                time.sleep(0.1)
        assert sorted(done) == ["e2e-1", "e2e-2"]
    finally:
        r.shutdown()
