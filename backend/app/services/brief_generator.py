"""Gemini-backed content brief for a recommendation (MVP)."""

from __future__ import annotations

import json
import re

from app.services.gemini_client import DEFAULT_MODEL, generate_text

SYSTEM = """You write concise, actionable content briefs for marketers.
Return valid JSON only with keys "title" (string) and "body" (string, markdown).
The body should include: objective, target audience, key angles, suggested H2 outline, trust/proof to add, and CTA ideas."""


def generate_brief_markdown(
    *,
    recommendation_title: str,
    recommendation_rationale: str,
    primary_domain: str,
    industry: str | None,
    sample_prompts: list[str],
) -> dict[str, str]:
    prompts_block = "\n".join(f"- {t}" for t in sample_prompts[:8])
    user = f"""Company domain: {primary_domain}
Industry: {industry or "unspecified"}

Recommendation focus: {recommendation_title}
Why: {recommendation_rationale}

Related customer prompts from this audit:
{prompts_block}

Produce the JSON brief."""

    raw = generate_text(SYSTEM, user, model=DEFAULT_MODEL, temperature=0.45)

    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return {
            "title": f"Brief: {recommendation_title}",
            "body": raw.strip(),
        }
    title = str(data.get("title") or f"Brief: {recommendation_title}").strip()
    body = str(data.get("body") or "").strip()
    if not body:
        body = raw.strip()
    return {"title": title, "body": body}
