"""Technical AI-visibility checks: the machine-readability layer.

Audit-only tools stop at "your content is weak". These checks cover the other
half: whether AI crawlers can even parse the site. Each check returns a
(passed, detail) pair and rolls into a fix list that maps 1:1 onto the rewrite
engine's artifact types.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


async def check_llms_txt(domain: str, *, timeout: float = 15.0) -> dict[str, Any]:
    url = f"https://{domain}/llms.txt"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and len(resp.text) > 50:
            return {"check": "llms_txt", "passed": True, "detail": f"Found ({len(resp.text)} chars)", "url": url}
        return {"check": "llms_txt", "passed": False,
                "detail": f"Missing or empty (HTTP {resp.status_code}). AI crawlers have no curated map of the site.",
                "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"check": "llms_txt", "passed": False, "detail": f"Fetch failed: {exc}", "url": url}


def check_jsonld(homepage_html: str) -> dict[str, Any]:
    blocks = _JSONLD_RE.findall(homepage_html or "")
    types: list[str] = []
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                t = item.get("@type")
                types.extend(t if isinstance(t, list) else [t] if t else [])
                graph = item.get("@graph")
                if isinstance(graph, list):
                    types.extend(str(g.get("@type")) for g in graph if isinstance(g, dict) and g.get("@type"))
    wanted = {"Organization", "Product", "SoftwareApplication", "FAQPage", "BreadcrumbList"}
    found = set(types)
    missing = sorted(wanted - found)
    return {
        "check": "jsonld_schema",
        "passed": bool(found & wanted) and "FAQPage" in found,
        "detail": (f"Schema types found: {sorted(found) or 'none'}. "
                   f"Missing high-value types: {missing or 'none'}."),
        "schema_types": sorted(found),
        "missing_types": missing,
    }


def check_entity_clarity(distilled: dict[str, Any]) -> dict[str, Any]:
    """Can an AI answer 'what is X, who is it for' from the homepage alone?
    Uses the fields crawler.distill_site_context already produces."""
    category = (distilled.get("product_category") or distilled.get("category") or "").strip()
    summary = (distilled.get("summary") or "").strip()
    audiences = distilled.get("audiences") or []
    problems = []
    if not category:
        problems.append("no clear product category statement")
    if len(summary.split()) < 12:
        problems.append("homepage summary too thin to quote")
    if not audiences:
        problems.append("target customer never named")
    return {
        "check": "entity_clarity",
        "passed": not problems,
        "detail": ("Entity is quotable: category, summary, audience all present."
                   if not problems else "Entity unclear: " + "; ".join(problems) + "."),
        "problems": problems,
    }


def check_page_coverage(page_type_counts: dict[str, int]) -> dict[str, Any]:
    missing = [t for t in ("faq", "comparison", "pricing", "reviews") if not page_type_counts.get(t)]
    return {
        "check": "page_coverage",
        "passed": not missing,
        "detail": ("All high-intent page types present." if not missing
                   else f"Missing page types AI engines lean on: {', '.join(missing)}."),
        "missing_page_types": missing,
    }


async def run_tech_checks(
    domain: str,
    homepage_html: str,
    distilled: dict[str, Any],
    page_type_counts: dict[str, int],
) -> list[dict[str, Any]]:
    return [
        await check_llms_txt(domain),
        check_jsonld(homepage_html),
        check_entity_clarity(distilled),
        check_page_coverage(page_type_counts),
    ]
