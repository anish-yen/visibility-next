from __future__ import annotations

import json
from typing import Any

import stripe

from app.config import get_settings


class StripeNotConfigured(RuntimeError):
    """Raised when a Stripe secret is required but missing."""


def _client() -> stripe.StripeClient:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not configured")
    return stripe.StripeClient(api_key=settings.stripe_secret_key)


def create_audit_checkout_session(
    *,
    user_id: str,
    primary_domain: str,
    competitor_domains: list[str],
    industry: str | None,
) -> str:
    """Create a one-time Checkout Session for a single paid audit.

    Audit inputs travel in session metadata rather than a pre-created DB row:
    the audit itself is only created once payment is confirmed, by the
    webhook handler reading this same metadata back out.
    """
    settings = get_settings()
    if not settings.stripe_price_id:
        raise StripeNotConfigured("STRIPE_PRICE_ID is not configured")

    client = _client()
    session = client.checkout.sessions.create(
        {
            "mode": "payment",
            "line_items": [{"price": settings.stripe_price_id, "quantity": 1}],
            "success_url": f"{settings.frontend_url}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{settings.frontend_url}/?checkout=cancelled",
            "metadata": {
                "user_id": user_id,
                "primary_domain": primary_domain,
                "competitor_domains": ",".join(competitor_domains),
                "industry": industry or "",
            },
        }
    )
    if not session.url:
        raise StripeNotConfigured("Stripe did not return a checkout URL")
    return session.url


def as_plain_data(value: Any) -> Any:
    """Recursively convert Stripe SDK objects into plain dicts/lists.

    `StripeObject` (and its subclasses `Event`, `checkout.Session`, ...) is NOT
    a dict in stripe>=16 and has no `.get`; calling it raises
    `AttributeError: 'get' is a dict method, but a Session is not a dict`.
    `to_dict()` is only shallow, so nested values need the same treatment -
    hence the recursion rather than a single conversion call.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: as_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_plain_data(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return {k: as_plain_data(v) for k, v in to_dict().items()}
    return value


def parse_webhook_event(payload: bytes, signature_header: str) -> dict[str, Any]:
    """Verify the signature, then return the event as plain nested dicts.

    The SDK verifies authenticity; the data is then read back out of the raw
    bytes it just verified, so nothing downstream depends on `StripeObject`
    semantics. Previously this returned the SDK object and callers used
    `.get()` on it, which raises on stripe>=16 and made every real webhook
    delivery a 500 - while tests that faked this function with a plain dict
    passed, because dicts do have `.get`.

    Only parsed AFTER construct_event returns: an unverified payload must
    never be read as if it were trustworthy.
    """
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not configured")
    event = stripe.Webhook.construct_event(
        payload, signature_header, settings.stripe_webhook_secret
    )
    try:
        return json.loads(payload)
    except ValueError:
        # Signature verified but the body is not JSON we can re-read; fall
        # back to converting the SDK object rather than failing the delivery.
        return as_plain_data(event)


def audit_inputs_from_session(session: Any) -> dict[str, Any] | None:
    """Pull the audit-creation inputs back out of a completed session's metadata.

    Accepts a plain dict or a Stripe SDK object: it normalizes first, so a
    caller that passes the SDK object straight through cannot reintroduce the
    `.get`-on-StripeObject crash.

    Returns None if the session is missing the fields we need (defensive -
    should not happen for sessions this app created, but a webhook handler
    must never crash on an unexpected payload).
    """
    session = as_plain_data(session)
    if not isinstance(session, dict):
        return None
    metadata = session.get("metadata") or {}
    user_id = metadata.get("user_id")
    primary_domain = metadata.get("primary_domain")
    if not user_id or not primary_domain:
        return None
    competitor_domains = [
        d.strip() for d in (metadata.get("competitor_domains") or "").split(",") if d.strip()
    ]
    return {
        "user_id": user_id,
        "primary_domain": primary_domain,
        "competitor_domains": competitor_domains,
        "industry": metadata.get("industry") or None,
    }
