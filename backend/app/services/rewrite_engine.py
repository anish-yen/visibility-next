"""Evidence-driven rewrite engine.

Why this exists: the current audit generates a generic "brief" from a template
(generate_content_brief). Every client gets the same five recommendations with
different nouns. That is what makes the output feel formulaic.

This engine does the opposite. For each weak area it:
  1. Takes the pages the answer engines ACTUALLY CITED for those exact prompts
     (from citations.build_citation_map).
  2. Fetches and dissects 1-3 winning pages: heading structure, first-100-word
     answer pattern, comparison tables, FAQ blocks, proof points, entities named.
  3. Feeds that evidence + the client's own crawled facts into Gemini and asks
     for a rewritten artifact that matches the winning pattern but says only
     true things about the client.

Hard rule enforced in the prompt: never invent facts. Anything the crawl did
not establish comes out as a [CONFIRM: ...] placeholder for the founder to
fill, so the rewrite is shippable but never fiction.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.crawler import _fetch_html, _extract_page, distill_site_context
from app.services.gemini_client import GeminiClient, GeminiError

MAX_EVIDENCE_PAGES = 3
MAX_EVIDENCE_CHARS = 4000   # per winning page, keeps prompts bounded
MAX_ARTIFACTS_PER_CYCLE = 3  # rewrite the worst gaps first; the loop re-prioritizes


def _dissect_winning_page(page: dict[str, Any]) -> dict[str, Any]:
    """Pull the structural signals that make a page citable."""
    headings = page.get("headings", [])
    text = (page.get("content_text") or "")[:MAX_EVIDENCE_CHARS]
    return {
        "url": page.get("url"),
        "page_type": page.get("page_type"),
        "title": page.get("title"),
        "heading_skeleton": headings[:15],
        "opens_with_direct_answer": bool(text and len(text.split()) > 30),
        "opening_excerpt": " ".join(text.split()[:80]),
        "body_excerpt": text,
    }


async def gather_winning_evidence(winning_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch and dissect the top cited pages that are NOT the client's own."""
    evidence: list[dict[str, Any]] = []
    for src in winning_sources:
        if src.get("is_owned"):
            continue
        if len(evidence) >= MAX_EVIDENCE_PAGES:
            break
        url = f"https://{src['domain']}"
        try:
            html = await _fetch_html(url)
            page = _extract_page(url, html)
            evidence.append(_dissect_winning_page(page))
        except Exception:  # noqa: BLE001 - a dead source just drops out
            continue
    return evidence


ARTIFACT_FOR_GAP = {
    "entity_clarity": "homepage_positioning",
    "jsonld_schema": "schema_pack",
    "llms_txt": "llms_txt",
    "faq": "faq_block",
    "comparison": "comparison_page",
    "pricing": "pricing_clarity_section",
    "trust": "proof_section",
}


def pick_artifacts(
    weak_buckets: dict[str, float],
    failed_checks: list[dict[str, Any]],
    citation_map: dict[str, Any],
) -> list[str]:
    """Choose which artifacts to build this cycle, worst gaps first."""
    wanted: list[str] = []
    for check in failed_checks:
        name = check.get("check", "")
        if name in ARTIFACT_FOR_GAP:
            wanted.append(ARTIFACT_FOR_GAP[name])
        for pt in check.get("missing_page_types", []):
            if pt in ARTIFACT_FOR_GAP:
                wanted.append(ARTIFACT_FOR_GAP[pt])
    # low-scoring prompt buckets map to content artifacts too
    bucket_to_artifact = {
        "informational": "faq_block",
        "comparative": "comparison_page",
        "pricing": "pricing_clarity_section",
        "trust": "proof_section",
    }
    for bucket, score in sorted(weak_buckets.items(), key=lambda kv: kv[1]):
        if score < 0.6 and bucket in bucket_to_artifact:
            wanted.append(bucket_to_artifact[bucket])
    # if nothing cited the client at all, positioning comes first
    if citation_map.get("owned_citation_share", 1.0) < 0.05:
        wanted.insert(0, "homepage_positioning")
    # dedupe, keep order, cap
    seen: set[str] = set()
    out = [a for a in wanted if not (a in seen or seen.add(a))]
    return out[:MAX_ARTIFACTS_PER_CYCLE]


async def generate_artifact(
    artifact_type: str,
    *,
    client_distilled: dict[str, Any],
    weak_prompts: list[dict[str, Any]],
    winning_evidence: list[dict[str, Any]],
    brand_label: str,
) -> dict[str, Any]:
    """One artifact, grounded in winning-page evidence and client facts."""
    client = GeminiClient()
    client_facts = {
        "category": client_distilled.get("product_category") or client_distilled.get("category"),
        "summary": client_distilled.get("summary"),
        "keywords": client_distilled.get("keywords", [])[:15],
        "use_cases": client_distilled.get("use_cases", [])[:10],
        "audiences": client_distilled.get("audiences", [])[:5],
        "trust_signals": client_distilled.get("trust_signals", [])[:5],
        "page_types_present": client_distilled.get("page_type_counts", {}),
    }
    system_instruction = (
        "You rewrite web content so AI answer engines (ChatGPT, Perplexity, Google AI) "
        "cite and recommend the product. You work from EVIDENCE: the structure of pages "
        "these engines already cite, and verified facts about the client. "
        "You never invent facts, numbers, customers, or claims. Return strict JSON only."
    )
    user_prompt = f"""
Artifact to produce: {artifact_type}
Client brand: {brand_label}

VERIFIED CLIENT FACTS (the only claims you may make):
{json.dumps(client_facts, indent=2, ensure_ascii=True)}

BUYER PROMPTS THE CLIENT CURRENTLY LOSES (the artifact must make these winnable):
{json.dumps([p.get('text') for p in weak_prompts[:8]], indent=2, ensure_ascii=True)}

WINNING PAGES THE AI ENGINES CITE FOR THESE PROMPTS (study the pattern, not the words):
{json.dumps(winning_evidence, indent=2, ensure_ascii=True)[:8000]}

Instructions:
- Mirror the STRUCTURE that wins citations (direct answer in the first sentence,
  specific category + audience naming, comparison/FAQ formatting, quotable one-liners).
- Write it for THIS company using only the verified facts above. No template filler.
- Any fact you need but do not have becomes a [CONFIRM: ...] placeholder. Never guess.
- Output must be ready to paste: real copy, not advice about copy.

Return JSON exactly like:
{{
  "artifact_type": "{artifact_type}",
  "format": "markdown|html|json-ld|plain-text",
  "content": "the full rewritten artifact",
  "changes_vs_pattern": ["why each structural choice matches the winning evidence"],
  "placeholders": ["list of [CONFIRM: ...] items the founder must fill"]
}}

Artifact format guide:
- homepage_positioning: rewritten hero H1 + 2-sentence category/audience statement + 3 proof bullets (markdown)
- faq_block: 6-8 buyer Q&As as markdown PLUS a valid FAQPage JSON-LD block (html)
- llms_txt: complete llms.txt file content (plain-text)
- comparison_page: full comparison page draft with table, honest about tradeoffs (markdown)
- pricing_clarity_section: pricing explainer section (markdown)
- proof_section: trust/proof section skeleton using only confirmed proof (markdown)
- schema_pack: Organization + SoftwareApplication JSON-LD for the homepage (json-ld)
"""
    try:
        data = await client.generate_json(
            system_instruction=system_instruction,
            user_prompt=user_prompt,
            temperature=0.45,
        )
        if data.get("content"):
            return {
                "artifact_type": artifact_type,
                "format": data.get("format", "markdown"),
                "content": str(data["content"]),
                "changes_vs_pattern": data.get("changes_vs_pattern", []),
                "placeholders": data.get("placeholders", []),
                "grounded_in": [e.get("url") for e in winning_evidence],
                "status": "generated",
            }
    except GeminiError as exc:
        return {"artifact_type": artifact_type, "status": "failed", "error": str(exc)}
    return {"artifact_type": artifact_type, "status": "failed", "error": "empty content"}
