"""
Simulated visibility evaluation: Gemini answers as a helpful assistant; we score mention of the target brand.
This is a directional, controlled pipeline — not a measurement of live ChatGPT/Gemini/Perplexity rankings.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.gemini_client import DEFAULT_MODEL, generate_text

logger = logging.getLogger(__name__)

SYSTEM_ANSWER = """You are a helpful AI assistant answering a customer's question naturally and concisely.
When relevant companies, products, or services would help the user, mention them by name.
Do not invent fake citations or URLs. Stay grounded in the context provided when it is relevant."""

EVAL_SYSTEM = """You judge how a simulated assistant answer reflects brand visibility for an audit tool.
Return ONLY valid JSON with keys:
- "mentioned": boolean — true if the target company/brand is clearly referenced by name or unmistakable shorthand
- "score": number — 1.0 if clearly/prominently mentioned, 0.5 if brief/weak mention, 0.0 if not mentioned
- "competitor_mentions": array of competitor brand/domain strings that appear in the answer (subset of provided list)
- "answer_summary": string — one short neutral summary of the answer (max ~400 chars)

No markdown, no code fences."""


def _brand_from_domain(domain: str) -> str:
    part = domain.split(".")[0]
    return part.replace("-", " ").title()


def _compact_context(pages: list[dict[str, Any]], domain: str, max_chars: int = 10_000) -> str:
    primary = [p for p in pages if p.get("domain") == domain]
    use = primary or pages
    parts: list[str] = []
    n = 0
    for p in use[:12]:
        line = f"{p.get('title','')}: {(p.get('content_text') or '')[:1500]}\n"
        if n + len(line) > max_chars:
            break
        parts.append(line)
        n += len(line)
    return "\n".join(parts) if parts else "(No crawled text.)"


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _parse_eval_json(raw: str) -> dict[str, Any]:
    return json.loads(_strip_json_fence(raw))


def simulate_answer_and_score(
    *,
    prompt_text: str,
    target_domain: str,
    competitor_domains: list[str],
    crawled_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Produce simulated answer + structured scores for one prompt.
    """
    brand = _brand_from_domain(target_domain)
    context = _compact_context(crawled_pages, target_domain)
    comp_list = ", ".join(competitor_domains) if competitor_domains else "(none)"

    user_answer = f"""Context from the company's crawled pages (may be incomplete):
{context}

Customer prompt:
{prompt_text}

Known competitors for this audit: {comp_list}
Target company domain: {target_domain} (brand often referred to as "{brand}")"""

    raw_answer = generate_text(
        SYSTEM_ANSWER,
        user_answer,
        model=DEFAULT_MODEL,
        temperature=0.55,
    )

    eval_user = f"""Target company domain: {target_domain}
Target brand label: {brand}
Competitor domains list: {comp_list}

Simulated assistant answer to score:
---
{raw_answer}
---
Original customer prompt: {prompt_text}"""

    raw_eval = generate_text(EVAL_SYSTEM, eval_user, model=DEFAULT_MODEL, temperature=0.1)
    try:
        ev = _parse_eval_json(raw_eval)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Eval JSON parse failed, using defaults: %s", e)
        mentioned = brand.lower() in raw_answer.lower() or target_domain.split(".")[0].lower() in raw_answer.lower()
        ev = {
            "mentioned": mentioned,
            "score": 1.0 if mentioned else 0.0,
            "competitor_mentions": [],
            "answer_summary": raw_answer[:400],
        }

    score = float(ev.get("score", 0.0))
    if score not in (0.0, 0.5, 1.0):
        if score >= 0.75:
            score = 1.0
        elif score >= 0.25:
            score = 0.5
        else:
            score = 0.0

    mentioned = bool(ev.get("mentioned", score > 0))
    comps = ev.get("competitor_mentions")
    if not isinstance(comps, list):
        comps = []
    comps_clean = [str(c).strip() for c in comps if str(c).strip()]

    summary = str(ev.get("answer_summary") or raw_answer[:400]).strip()

    return {
        "raw_answer": raw_answer,
        "mentioned": mentioned,
        "score": score,
        "competitor_mentions": comps_clean,
        "answer_summary": summary,
    }
