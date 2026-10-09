from __future__ import annotations

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


def parse_webhook_event(payload: bytes, signature_header: str) -> stripe.Event:
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not configured")
    return stripe.Webhook.construct_event(
        payload, signature_header, settings.stripe_webhook_secret
    )


def audit_inputs_from_session(session: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the audit-creation inputs back out of a completed session's metadata.

    Returns None if the session is missing the fields we need (defensive -
    should not happen for sessions this app created, but a webhook handler
    must never crash on an unexpected payload).
    """
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
