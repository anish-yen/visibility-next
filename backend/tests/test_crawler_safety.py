"""SSRF guard: the crawler must never open a socket to a non-public address.

Covers PLAN_OF_ACTION.md 1.5.d - loopback, cloud metadata (169.254.169.254),
and a public->internal redirect are all refused. The redirect case is the one
that matters most: the submitted host is perfectly public, so only per-hop
revalidation catches it.

These tests assert on *refusal before connection*, not just on an error, by
recording every host a transport is actually asked to connect to.
"""
import asyncio

import httpx
import pytest

from app.services import crawler, safe_fetch, tech_checks
from app.services.safe_fetch import UnsafeURLError

PUBLIC_IP = "93.184.216.34"
INTERNAL_HOSTS = {"127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1"}


@pytest.fixture
def connections(monkeypatch):
    """Patch DNS + transport; return the list of hosts actually connected to."""
    attempted: list[str] = []

    async def fake_resolve(host: str) -> list[str]:
        if host in INTERNAL_HOSTS:
            # Simulate a hostname that resolves into private space.
            return {"localhost": ["127.0.0.1"]}.get(host, [host])
        return [PUBLIC_IP]

    monkeypatch.setattr(safe_fetch, "_resolve", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.host)
        return httpx.Response(200, text="<html><title>ok</title><body>fine</body></html>")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(safe_fetch.httpx, "AsyncClient", fake_client)
    return attempted


# --- direct address validation -----------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/",
    "https://localhost/",
    "http://169.254.169.254/latest/meta-data/",
    "https://10.0.0.5/",
    "http://192.168.1.1/admin",
])
def test_assert_public_url_refuses_internal(url, connections):
    with pytest.raises(UnsafeURLError):
        asyncio.run(safe_fetch.assert_public_url(url))
    assert connections == []


def test_assert_public_url_allows_public(connections):
    asyncio.run(safe_fetch.assert_public_url("https://example.com/"))
    assert connections == []  # validation alone opens no socket


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/"])
def test_assert_public_url_refuses_non_http_schemes(url, connections):
    with pytest.raises(UnsafeURLError):
        asyncio.run(safe_fetch.assert_public_url(url))
    assert connections == []


def test_ip_literal_is_checked_without_dns(monkeypatch):
    """An IP literal must be rejected on its own value, not via a resolver
    that a caller could stub out."""
    async def explode(host):
        raise AssertionError(f"should not resolve an IP literal: {host}")

    monkeypatch.setattr(safe_fetch, "_resolve", explode)
    with pytest.raises(UnsafeURLError):
        asyncio.run(safe_fetch.assert_public_url("http://169.254.169.254/"))


# --- redirects ----------------------------------------------------------

def test_redirect_into_private_space_is_refused(monkeypatch):
    """Public host 302s to the metadata endpoint. httpx's own follow_redirects
    would have fetched it; the manual per-hop loop must not."""
    attempted: list[str] = []

    async def fake_resolve(host: str) -> list[str]:
        return ["169.254.169.254"] if host == "evil.example.com" else [PUBLIC_IP]

    monkeypatch.setattr(safe_fetch, "_resolve", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.host)
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="SECRET")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        safe_fetch.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    with pytest.raises(UnsafeURLError):
        asyncio.run(safe_fetch.safe_get("https://public.example.com/"))

    assert "public.example.com" in attempted
    assert "169.254.169.254" not in attempted  # never connected to


def test_redirect_chain_is_capped(monkeypatch):
    async def fake_resolve(host: str) -> list[str]:
        return [PUBLIC_IP]

    monkeypatch.setattr(safe_fetch, "_resolve", fake_resolve)
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        safe_fetch.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    with pytest.raises(UnsafeURLError, match="redirects"):
        asyncio.run(safe_fetch.safe_get("https://example.com/"))
    assert len(hops) == safe_fetch.MAX_REDIRECTS + 1


# --- body size cap ------------------------------------------------------

def test_oversized_body_is_refused(monkeypatch):
    async def fake_resolve(host: str) -> list[str]:
        return [PUBLIC_IP]

    monkeypatch.setattr(safe_fetch, "_resolve", fake_resolve)
    monkeypatch.setattr(safe_fetch, "MAX_RESPONSE_BYTES", 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        safe_fetch.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    with pytest.raises(UnsafeURLError, match="exceeded"):
        asyncio.run(safe_fetch.safe_get("https://example.com/"))


# --- crawler integration ------------------------------------------------

@pytest.mark.parametrize("domain", ["127.0.0.1:8000", "169.254.169.254", "localhost"])
def test_crawl_site_refuses_internal_domain(domain, connections):
    site = asyncio.run(crawler.crawl_site(domain))

    assert site["status"] == "refused"
    assert site["pages"] == []
    assert "non-public" in site["error"]
    # the whole point: no socket was ever opened to the internal host
    assert connections == []


def test_crawl_site_still_works_for_public_domain(connections):
    site = asyncio.run(crawler.crawl_site("example.com"))

    assert site["status"] != "refused"
    assert connections  # a public host is genuinely fetched
    assert all(h not in INTERNAL_HOSTS for h in connections)


def test_llms_txt_check_refuses_internal_domain(connections):
    result = asyncio.run(tech_checks.check_llms_txt("169.254.169.254"))

    assert result["passed"] is False
    assert connections == []


# --- rejected before payment -------------------------------------------

def _checkout_app(monkeypatch):
    """Minimal app over the real router, QA-whitelisted so no Stripe call is
    needed - the point is that validation happens before either branch."""
    from fastapi import FastAPI
    from app.config import get_settings
    from app.routers import audits as audits_router

    monkeypatch.setenv("QA_BYPASS_EMAILS", "qa@example.com")
    get_settings.cache_clear()

    async def no_run(audit_id):
        return None

    monkeypatch.setattr(audits_router.audit_pipeline, "run_audit", no_run)

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user_id = "u1"
        request.state.user = {"email": "qa@example.com"}
        return await call_next(request)

    app.include_router(audits_router.router)
    return app


@pytest.mark.parametrize("payload", [
    {"primary_domain": "127.0.0.1:8000", "competitor_domains": []},
    {"primary_domain": "169.254.169.254", "competitor_domains": []},
    # a public primary with an internal competitor must be rejected too
    {"primary_domain": "example.com", "competitor_domains": ["10.0.0.5"]},
])
def test_checkout_rejects_non_public_domains_before_charging(payload, connections, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(_checkout_app(monkeypatch))
    try:
        r = client.post("/checkout", json=payload)
        assert r.status_code == 400
        assert "public website address" in r.json()["detail"]
        assert connections == []
    finally:
        from app.config import get_settings
        get_settings.cache_clear()


def test_checkout_accepts_public_domains(connections, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(_checkout_app(monkeypatch))
    try:
        r = client.post(
            "/checkout",
            json={"primary_domain": "example.com", "competitor_domains": ["rival.com"]},
        )
        assert r.status_code == 200
        assert r.json()["bypassed"] is True
    finally:
        from app.config import get_settings
        get_settings.cache_clear()
