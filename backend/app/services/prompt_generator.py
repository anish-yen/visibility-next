"""
Generate audit prompts via Gemini (JSON-only). Controlled simulation inputs — not live engine queries.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from app.services.gemini_client import DEFAULT_MODEL, generate_text

logger = logging.getLogger(__name__)

SYSTEM = """You generate realistic customer search prompts for AI answer engines like ChatGPT, Gemini, and Perplexity.
Return valid JSON only — no markdown fences, no commentary.
The JSON must be a single array of objects, each with:
- "text": string (the natural-language prompt a buyer might type)
- "intent": one of "informational", "comparative", "transactional", "trust"
Cover a mix of: informational, comparative, transactional/commercial, and trust/review-oriented intents."""

USER_TEMPLATE = """Company primary domain: {domain}
Industry context (may be empty): {industry}
Competitor domains (may be empty): {competitors}

Homepage and key page summaries (from crawl, may be truncated):
{summaries}

Generate exactly {count} varied, realistic prompts. Return ONLY a JSON array like:
[{{"text":"...","intent":"informational"}}, ...]"""


def _summarize_pages_for_prompt(pages: list[dict[str, Any]], max_chars: int = 14_000) -> str:
    lines: list[str] = []
    n = 0
    for p in pages:
        title = p.get("title") or ""
        ptype = p.get("page_type") or ""
        url = p.get("url") or ""
        body = (p.get("content_text") or "")[:2_000]
        chunk = f"---\nURL: {url}\nType: {ptype}\nTitle: {title}\n{body}\n"
        if n + len(chunk) > max_chars:
            break
        lines.append(chunk)
        n += len(chunk)
    return "\n".join(lines) if lines else "(No page text captured from crawl.)"


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _parse_prompt_json(raw: str) -> list[dict[str, Any]]:
    s = _strip_json_fence(raw)
    data = json.loads(s)
    if not isinstance(data, list):
        raise ValueError("Expected JSON array")
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        intent = str(item.get("intent", "informational")).strip().lower()
        if intent not in ("informational", "comparative", "transactional", "trust"):
            intent = "informational"
        if text:
            out.append({"text": text, "intent": intent})
    return out


def generate_audit_prompts(
    *,
    primary_domain: str,
    competitor_domains: list[str],
    industry: str | None,
    crawled_pages: list[dict[str, Any]],
    count: int = 15,
) -> list[dict[str, Any]]:
    """
    Returns list of {id, text, intent} ready for visibility evaluation (scores filled later).
    """
    bare = primary_domain
    primary_pages = [p for p in crawled_pages if p.get("domain") == bare]
    summaries = _summarize_pages_for_prompt(primary_pages or crawled_pages)
    competitors = ", ".join(competitor_domains) if competitor_domains else "(none)"

    user = USER_TEMPLATE.format(
        domain=primary_domain,
        industry=industry or "",
        competitors=competitors,
        summaries=summaries,
        count=count,
    )

    raw = generate_text(SYSTEM, user, model=DEFAULT_MODEL, temperature=0.5)
    try:
        parsed = _parse_prompt_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Invalid JSON from Gemini on first prompt generation: %s", e)
        repair = (
            "Your previous output was not valid JSON. Output ONLY a JSON array of "
            f'{count} objects with keys "text" and "intent". No markdown. '
            f"Valid intents: informational, comparative, transactional, trust.\n\n"
            f"Original request context domain: {primary_domain}\n"
            f"Broken output to fix (extract/fix into valid JSON only):\n{raw[:8000]}"
        )
        raw2 = generate_text(SYSTEM, repair, model=DEFAULT_MODEL, temperature=0.2)
        parsed = _parse_prompt_json(raw2)

    if not parsed:
        raise RuntimeError("Gemini returned no usable prompts after repair")

    results: list[dict[str, Any]] = []
    for row in parsed[:count]:
        results.append(
            {
                "id": str(uuid.uuid4()),
                "text": row["text"],
                "intent": row["intent"],
            }
        )
    return results
