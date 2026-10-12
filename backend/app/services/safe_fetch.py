"""Public-address-only HTTP fetching for the crawler.

Why this exists: the crawler fetches URLs derived from user-submitted domains.
Without a guard, a user can point an audit at `127.0.0.1:8000`,
`169.254.169.254` (cloud instance metadata), or any RFC1918 host and make the
backend fetch it from inside the network perimeter - a server-side request
forgery. Following redirects re-opens the same hole even when the submitted
host is public, because the redirect target is attacker-controlled.

So every hop is checked the same way: resolve the hostname, then require that
EVERY resolved address is globally routable. `not is_global` is the rejection
test rather than `is_private` because it also covers carrier-grade NAT
(100.64/10), reserved space, and IPv4-mapped IPv6 like `::ffff:127.0.0.1`.

Residual risk, deliberately not solved here: DNS rebinding. We resolve and
validate, then hand the hostname to httpx, which resolves again on connect -
a name that returns a public address on the first lookup and a private one on
the second would slip through. Closing that needs pinning the connection to
the validated IP; it is a narrower, higher-effort attack than the wide-open
hole this module closes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 2_500_000  # ~2.5MB of HTML is already far more than we parse
ALLOWED_SCHEMES = ("http", "https")


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a non-public address, or is malformed."""


@dataclass
class FetchResult:
    status_code: int
    text: str
    final_url: str


async def _resolve(host: str) -> list[str]:
    def _lookup() -> list[str]:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return [info[4][0] for info in infos]

    try:
        return await asyncio.to_thread(_lookup)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {host!r}") from exc


async def assert_public_url(url: str) -> None:
    """Raise UnsafeURLError unless url is http(s) and every address its host
    resolves to is globally routable."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parts.scheme!r} is not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"no host in URL {url!r}")

    # An IP literal needs no DNS lookup - check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = await _resolve(host)
    else:
        addresses = [str(literal)]

    if not addresses:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")

    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeURLError(f"host {host!r} resolved to unparseable {address!r}") from exc
        if not parsed.is_global:
            raise UnsafeURLError(
                f"host {host!r} resolves to non-public address {address} - refusing to fetch"
            )


async def safe_get(
    url: str,
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET url, validating the address before every hop and capping body size.

    Redirects are handled here rather than by httpx so each hop goes through
    assert_public_url first; httpx's own follow_redirects would jump straight
    to an attacker-chosen internal host.
    """
    current = url
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers=headers or {}
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await assert_public_url(current)
            body = bytearray()
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeURLError(f"redirect from {current!r} had no Location")
                    current = urljoin(current, location)
                    continue
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise UnsafeURLError(
                            f"response from {current!r} exceeded {MAX_RESPONSE_BYTES} bytes"
                        )
                encoding = response.encoding or "utf-8"
                return FetchResult(
                    status_code=response.status_code,
                    text=bytes(body).decode(encoding, errors="replace"),
                    final_url=current,
                )
    raise UnsafeURLError(f"more than {MAX_REDIRECTS} redirects starting from {url!r}")
