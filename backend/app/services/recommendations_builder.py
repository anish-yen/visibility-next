"""
MVP content-gap recommendations from crawled page types + weak prompt intents.
"""

from __future__ import annotations

from typing import Any


def _types_on_primary(pages: list[dict[str, Any]], primary_domain: str) -> set[str]:
    return {
        str(p.get("page_type") or "")
        for p in pages
        if p.get("domain") == primary_domain and p.get("page_type")
    }


def _avg_score_for_intent(prompt_rows: list[dict[str, Any]], intent: str) -> float | None:
    subset = [p for p in prompt_rows if p.get("intent") == intent]
    if not subset:
        return None
    return sum(float(p.get("score", 0)) for p in subset) / len(subset)


def build_recommendations(
    *,
    audit_id: str,
    primary_domain: str,
    crawled_pages: list[dict[str, Any]],
    prompt_rows: list[dict[str, Any]],
    industry: str | None,
) -> list[dict[str, Any]]:
    types_seen = _types_on_primary(crawled_pages, primary_domain)
    recs: list[dict[str, Any]] = []
    ind = industry or "your space"

    def add(title: str, rationale: str, base_priority: float) -> None:
        recs.append(
            {
                "id": f"rec-{audit_id[:8]}-{len(recs)}",
                "title": title,
                "rationale": rationale,
                "priority_score": round(base_priority, 2),
                "brief": None,
            }
        )

    comp_weak = _avg_score_for_intent(prompt_rows, "comparative")
    trust_weak = _avg_score_for_intent(prompt_rows, "trust")
    trans_weak = _avg_score_for_intent(prompt_rows, "transactional")
    info_weak = _avg_score_for_intent(prompt_rows, "informational")

    if "pricing" not in types_seen:
        p = 0.88
        if trans_weak is not None and trans_weak < 0.5:
            p += 0.05
        add(
            "Add or improve a pricing page",
            f"No dedicated pricing page type detected on {primary_domain}. "
            f"Transactional prompts often expect clear pricing signals in {ind}.",
            min(0.95, p),
        )

    if "faq" not in types_seen:
        p = 0.82
        if trust_weak is not None and trust_weak < 0.5:
            p += 0.06
        add(
            "Publish an FAQ or help hub",
            "FAQ/help pages support objection-handling prompts and trust-oriented AI answers.",
            min(0.94, p),
        )

    if "comparison" not in types_seen:
        p = 0.86
        if comp_weak is not None and comp_weak < 0.6:
            p += 0.07
        add(
            "Create comparison or alternative pages",
            "Comparative customer prompts benefit from explicit vs-competitor or alternative content.",
            min(0.96, p),
        )

    if "review" not in types_seen:
        p = 0.78
        if trust_weak is not None and trust_weak < 0.55:
            p += 0.06
        add(
            "Add reviews, testimonials, or social proof",
            "Trust-heavy prompts reference vendors with visible customer proof.",
            min(0.92, p),
        )

    if "blog" not in types_seen and info_weak is not None and info_weak < 0.55:
        add(
            "Expand educational/blog or resources content",
            "Informational prompts underperform; topical articles improve entity coverage in simulated answers.",
            0.72,
        )

    if not recs:
        add(
            "Tighten homepage positioning and meta clarity",
            "Crawl found common page types; focus on clearer H1/meta and entity consistency for AI-style answers.",
            0.65,
        )

    recs.sort(key=lambda r: r["priority_score"], reverse=True)
    for i, r in enumerate(recs):
        r["id"] = f"rec-{audit_id[:8]}-{i}"
    return recs
