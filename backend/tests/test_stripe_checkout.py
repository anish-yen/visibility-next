"""Stripe checkout gating: QA whitelist bypass, paid-audit gate, webhook fulfillment.

POST /audits requires the caller's email to be on QA_BYPASS_EMAILS (402 otherwise).
POST /checkout is the real entry point: whitelisted emails get an audit immediately,
everyone else gets a Stripe Checkout URL and the audit is only created once the
webhook confirms payment - never from the success page.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers import audits as audits_router
from app.services.stripe_client import StripeNotConfigured


@pytest.fixture(autouse=True)
def _no_real_audit_pipeline(monkeypatch):
    """_start_audit fires asyncio.create_task(audit_pipeline.run_audit(...)) -
    stub it so these gating/webhook tests never crawl real domains or call Gemini."""
    async def fake_run_audit(audit_id):
        return None

    monkeypatch.setattr(audits_router.audit_pipeline, "run_audit", fake_run_audit)


def _app(email="whitelisted@example.com", user_id="u1"):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user_id = request.headers.get("x-test-user", user_id)
        request.state.user = {"email": request.headers.get("x-test-email", email)}
        return await call_next(request)

    app.include_router(audits_router.router)
    return app


def test_is_qa_whitelisted_matches_lowercased_email(monkeypatch):
    monkeypatch.setenv("QA_BYPASS_EMAILS", "Test@Example.com")
    get_settings.cache_clear()
    try:
        class FakeRequest:
            state = type("S", (), {"user": {"email": "test@example.com"}})()

        assert audits_router._is_qa_whitelisted(FakeRequest()) is True

        class FakeRequestNoMatch:
            state = type("S", (), {"user": {"email": "nope@example.com"}})()

        assert audits_router._is_qa_whitelisted(FakeRequestNoMatch()) is False
    finally:
        get_settings.cache_clear()


def test_post_audits_requires_whitelist(monkeypatch):
    monkeypatch.setenv("QA_BYPASS_EMAILS", "allowed@example.com")
    get_settings.cache_clear()
    try:
        client = TestClient(_app(email="blocked@example.com"))
        r = client.post("/audits", json={"primary_domain": "acme.com", "competitor_domains": []})
        assert r.status_code == 402

        client = TestClient(_app(email="allowed@example.com"))
        r = client.post("/audits", json={"primary_domain": "acme.com", "competitor_domains": []})
        assert r.status_code == 201
        assert r.json()["primary_domain"] == "acme.com"
    finally:
        get_settings.cache_clear()


def test_checkout_bypasses_for_whitelisted_email(monkeypatch):
    monkeypatch.setenv("QA_BYPASS_EMAILS", "allowed@example.com")
    get_settings.cache_clear()
    try:
        client = TestClient(_app(email="allowed@example.com"))
        r = client.post("/checkout", json={"primary_domain": "acme.com", "competitor_domains": []})
        assert r.status_code == 200
        body = r.json()
        assert body["bypassed"] is True
        assert body["audit"]["primary_domain"] == "acme.com"
        assert body["checkout_url"] is None
    finally:
        get_settings.cache_clear()


def test_checkout_redirects_non_whitelisted_to_stripe(monkeypatch):
    monkeypatch.setenv("QA_BYPASS_EMAILS", "allowed@example.com")
    get_settings.cache_clear()

    captured = {}

    def fake_create(*, user_id, primary_domain, competitor_domains, industry):
        captured.update(
            user_id=user_id,
            primary_domain=primary_domain,
            competitor_domains=competitor_domains,
            industry=industry,
        )
        return "https://checkout.stripe.com/c/pay/test_session"

    monkeypatch.setattr(audits_router.stripe_client, "create_audit_checkout_session", fake_create)

    try:
        client = TestClient(_app(email="payer@example.com", user_id="u2"))
        r = client.post(
            "/checkout",
            json={"primary_domain": "acme.com", "competitor_domains": ["rival.com"], "industry": "SaaS"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bypassed"] is False
        assert body["audit"] is None
        assert body["checkout_url"] == "https://checkout.stripe.com/c/pay/test_session"
        assert captured == {
            "user_id": "u2",
            "primary_domain": "acme.com",
            "competitor_domains": ["rival.com"],
            "industry": "SaaS",
        }
    finally:
        get_settings.cache_clear()


def test_checkout_surfaces_stripe_not_configured_as_500(monkeypatch):
    monkeypatch.setenv("QA_BYPASS_EMAILS", "allowed@example.com")
    get_settings.cache_clear()

    def fake_create(**kwargs):
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not configured")

    monkeypatch.setattr(audits_router.stripe_client, "create_audit_checkout_session", fake_create)

    try:
        client = TestClient(_app(email="payer@example.com"))
        r = client.post("/checkout", json={"primary_domain": "acme.com", "competitor_domains": []})
        assert r.status_code == 500
    finally:
        get_settings.cache_clear()


def test_webhook_rejects_bad_signature(monkeypatch):
    def fake_parse(payload, signature_header):
        raise ValueError("signature mismatch")

    monkeypatch.setattr(audits_router.stripe_client, "parse_webhook_event", fake_parse)
    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "bad"})
    assert r.status_code == 400


def _fake_event(event_type, session):
    return {"type": event_type, "data": {"object": session}}


def test_webhook_fulfills_on_completed_when_paid(monkeypatch):
    started = []
    monkeypatch.setattr(audits_router, "_start_audit", lambda *a: started.append(a))

    session = {
        "id": "cs_test_1",
        "payment_status": "paid",
        "metadata": {"user_id": "u1", "primary_domain": "acme.com", "competitor_domains": "", "industry": ""},
    }
    monkeypatch.setattr(
        audits_router.stripe_client,
        "parse_webhook_event",
        lambda payload, sig: _fake_event("checkout.session.completed", session),
    )

    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})
    assert r.status_code == 200
    assert r.json() == {"received": True}
    assert len(started) == 1
    assert started[0][:2] == ("u1", "acme.com")


def test_webhook_does_not_fulfill_completed_while_unpaid(monkeypatch):
    started = []
    monkeypatch.setattr(audits_router, "_start_audit", lambda *a: started.append(a))

    session = {
        "id": "cs_test_2",
        "payment_status": "unpaid",
        "metadata": {"user_id": "u1", "primary_domain": "acme.com", "competitor_domains": "", "industry": ""},
    }
    monkeypatch.setattr(
        audits_router.stripe_client,
        "parse_webhook_event",
        lambda payload, sig: _fake_event("checkout.session.completed", session),
    )

    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})
    assert r.status_code == 200
    assert started == []


def test_webhook_fulfills_on_async_payment_succeeded(monkeypatch):
    started = []
    monkeypatch.setattr(audits_router, "_start_audit", lambda *a: started.append(a))

    session = {
        "id": "cs_test_3",
        "payment_status": "paid",
        "metadata": {"user_id": "u3", "primary_domain": "delayed.com", "competitor_domains": "", "industry": ""},
    }
    monkeypatch.setattr(
        audits_router.stripe_client,
        "parse_webhook_event",
        lambda payload, sig: _fake_event("checkout.session.async_payment_succeeded", session),
    )

    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})
    assert r.status_code == 200
    assert len(started) == 1
    assert started[0][:2] == ("u3", "delayed.com")


def test_webhook_logs_async_payment_failed_without_fulfilling(monkeypatch):
    started = []
    monkeypatch.setattr(audits_router, "_start_audit", lambda *a: started.append(a))

    session = {"id": "cs_test_4", "payment_status": "unpaid"}
    monkeypatch.setattr(
        audits_router.stripe_client,
        "parse_webhook_event",
        lambda payload, sig: _fake_event("checkout.session.async_payment_failed", session),
    )

    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})
    assert r.status_code == 200
    assert started == []


def test_webhook_handles_missing_metadata_without_crashing(monkeypatch, caplog):
    started = []
    monkeypatch.setattr(audits_router, "_start_audit", lambda *a: started.append(a))

    session = {"id": "cs_test_5", "payment_status": "paid", "metadata": {}}
    monkeypatch.setattr(
        audits_router.stripe_client,
        "parse_webhook_event",
        lambda payload, sig: _fake_event("checkout.session.completed", session),
    )

    client = TestClient(_app())
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})
    assert r.status_code == 200
    assert started == []
    assert "cs_test_5" in caplog.text
