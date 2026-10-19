"""Replay of the real production webhook that returned HTTP 500.

Event `evt_1US7BZRwvho4FlH7Iipglg6v`, `checkout.session.completed`,
`payment_status: paid`, signature valid, metadata carrying a uuid `user_id`,
`primary_domain: cal.com`, `competitor_domains: "calendly.com"` and
`industry: ""`.

Root cause: the handler called `.get()` on the object returned by
`stripe.Webhook.construct_event`. In stripe>=16 that is a
`stripe.checkout.Session`, which is NOT a dict and has no `.get` - it raises
`AttributeError: 'get' is a dict method, but a Session is not a dict`. The
first line to raise was `session.get("payment_status")` in `stripe_webhook`,
with `audit_inputs_from_session`'s `session.get("metadata")` next in line.

Why the existing suite missed it: `test_stripe_checkout.py` fakes
`parse_webhook_event` and returns a plain **dict**, and dicts do have `.get`.
So the tests exercised dict semantics that production never used. These tests
deliberately build a REAL signed event and let the REAL
`stripe.Webhook.construct_event` run, so the object the handler sees is the
same class production sees. No network call and no Stripe API call: signature
verification is pure local HMAC.
"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import audit_store
from app.config import get_settings
from app.routers import audits as audits_router
from app.services import stripe_client

WEBHOOK_SECRET = "whsec_replay_only_not_a_real_secret"
USER_ID = "19fc57e2-719d-4f00-ade8-87d3b1733778"

SESSION = {
    "id": "cs_test_b1US7BZRwvho4FlH7",
    "object": "checkout.session",
    "mode": "payment",
    "status": "complete",
    "payment_status": "paid",
    "amount_total": 14900,
    "currency": "usd",
    "metadata": {
        "user_id": USER_ID,
        "primary_domain": "cal.com",
        "competitor_domains": "calendly.com",
        "industry": "",
    },
}
EVENT = {
    "id": "evt_1US7BZRwvho4FlH7Iipglg6v",
    "object": "event",
    "api_version": "2026-08-26.dahlia",
    "type": "checkout.session.completed",
    "created": 1792300000,
    "livemode": False,
    "data": {"object": SESSION},
}


def _signed(event: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    """Build the body and a genuine Stripe-Signature header for it."""
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return payload, f"t={timestamp},v1={digest}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()

    started: list[str] = []
    real_start = audits_router._start_audit

    def spy_start_audit(user_id, primary_domain, competitor_domains, industry):
        started.append(user_id)
        return real_start(user_id, primary_domain, competitor_domains, industry)

    async def no_pipeline(audit_id):
        return None

    monkeypatch.setattr(audits_router.audit_pipeline, "run_audit", no_pipeline)
    monkeypatch.setattr(audits_router, "_start_audit", spy_start_audit)

    app = FastAPI()
    app.include_router(audits_router.router)
    test_client = TestClient(app)
    test_client.started = started  # type: ignore[attr-defined]
    try:
        yield test_client
    finally:
        get_settings.cache_clear()


def test_the_exact_production_event_returns_200_not_500(client):
    payload, signature = _signed(EVENT)

    response = client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": signature}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"received": True}


def test_the_replayed_event_actually_creates_the_audit(client):
    payload, signature = _signed(EVENT)

    client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": signature}
    )

    assert client.started == [USER_ID], "fulfillment must run for a paid session"
    audits = audit_store.list_for_user(USER_ID)
    assert len(audits) == 1
    created = audits[0]
    assert created.primary_domain == "cal.com"
    assert created.competitor_domains == ["calendly.com"]
    assert created.industry is None, 'empty-string industry must normalize to None'


def test_real_stripe_object_is_not_a_dict_and_has_no_get():
    """Pins the actual defect so a future refactor cannot quietly reintroduce
    it by going back to `.get()` on the SDK object."""
    payload, signature = _signed(EVENT)
    event_obj = __import__("stripe").Webhook.construct_event(
        payload, signature, WEBHOOK_SECRET
    )
    session_obj = event_obj["data"]["object"]

    assert not isinstance(session_obj, dict)
    assert not hasattr(session_obj, "get")
    with pytest.raises(AttributeError, match="is a dict method"):
        session_obj.get("payment_status")


def test_parse_webhook_event_returns_plain_nested_dicts(monkeypatch):
    """The fix: everything downstream of signature verification is plain data."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    try:
        payload, signature = _signed(EVENT)
        event = stripe_client.parse_webhook_event(payload, signature)

        assert isinstance(event, dict)
        session = event["data"]["object"]
        assert isinstance(session, dict) and hasattr(session, "get")
        assert isinstance(session["metadata"], dict)
        assert session.get("payment_status") == "paid"
    finally:
        get_settings.cache_clear()


def test_audit_inputs_survive_being_handed_the_raw_sdk_object():
    """Defence in depth: a caller passing the SDK object straight through must
    not resurrect the crash."""
    payload, signature = _signed(EVENT)
    session_obj = __import__("stripe").Webhook.construct_event(
        payload, signature, WEBHOOK_SECRET
    )["data"]["object"]

    inputs = stripe_client.audit_inputs_from_session(session_obj)

    assert inputs == {
        "user_id": USER_ID,
        "primary_domain": "cal.com",
        "competitor_domains": ["calendly.com"],
        "industry": None,
    }


def test_bad_signature_is_still_rejected_before_any_parsing(client):
    """The fix must not weaken verification: an unverified body is never read."""
    payload, _ = _signed(EVENT)

    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )

    assert response.status_code == 400
    assert client.started == [], "nothing may be fulfilled from an unverified body"


def test_unpaid_session_still_defers_fulfillment(client):
    event = json.loads(json.dumps(EVENT))
    event["data"]["object"]["payment_status"] = "unpaid"
    payload, signature = _signed(event)

    response = client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": signature}
    )

    assert response.status_code == 200
    assert client.started == [], "an unpaid session must not create an audit"


def test_async_payment_succeeded_also_fulfills(client):
    event = json.loads(json.dumps(EVENT))
    event["type"] = "checkout.session.async_payment_succeeded"
    payload, signature = _signed(event)

    client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": signature}
    )

    assert client.started == [USER_ID]


def test_missing_metadata_is_logged_not_crashed(client):
    event = json.loads(json.dumps(EVENT))
    event["data"]["object"]["metadata"] = {}
    payload, signature = _signed(event)

    response = client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": signature}
    )

    assert response.status_code == 200
    assert client.started == []
