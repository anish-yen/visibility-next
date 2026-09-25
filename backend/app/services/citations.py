"""Citation map: which sources the answer engines actually read for this category.

Aggregates the cited URLs from answer_engines.EngineAnswer across all prompts
in a cycle. The output drives two things:
  1. The gap analysis ("you are invisible because ChatGPT cites G2, Capterra
     and competitor blogs, and you appear on none of them").
  2. The rewrite engine, which fetches the winning cited pages and learns the
     structure that earns citations (direct answers, comparison tables, FAQs).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

# Coarse source-type buckets. Good enough for reporting; refine per-niche later.
_DIRECTORY_HINTS = ("g2.com", "capterra", "producthunt", "trustpilot", "getapp",
                    "softwareadvice", "alternativeto", "saasworthy", "appsumo")
_REVIEW_HINTS = ("review", "best-", "top-", "vs-", "comparison", "alternatives")
_UGC_HINTS = ("reddit.com", "news.ycombinator", "stackoverflow", "quora.com")


def _domain_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url.strip()).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def classify_source(url: str) -> str:
    d = _domain_of(url)
    path = url.lower()
    if any(h in d for h in _DIRECTORY_HINTS):
        return "directory"
    if any(h in d for h in _UGC_HINTS):
        return "community"
    if any(h in path for h in _REVIEW_HINTS):
        return "review_article"
    if d.endswith((".io", ".dev")) or "/docs" in path or "/blog" in path:
        return "vendor_content"
    return "other_site"


def build_citation_map(
    engine_answers: list[dict[str, Any]],   # serialized EngineAnswer rows with prompt intent
    *,
    owned_domains: set[str] | None = None,  # target + competitor domains, flagged separately
) -> dict[str, Any]:
    owned_domains = {d.lower() for d in (owned_domains or set())}
    per_domain: Counter[str] = Counter()
    per_domain_engines: dict[str, set[str]] = defaultdict(set)
    per_domain_intents: dict[str, Counter[str]] = defaultdict(Counter)
    type_counts: Counter[str] = Counter()
    total_citations = 0

    for ans in engine_answers:
        if ans.get("error"):
            continue
        engine = ans.get("engine", "unknown")
        intent = ans.get("intent") or "other"
        for url in ans.get("cited_urls") or []:
            d = _domain_of(url)
            if not d:
                continue
            total_citations += 1
            per_domain[d] += 1
            per_domain_engines[d].add(engine)
            per_domain_intents[d][intent] += 1
            type_counts[classify_source(url)] += 1

    winning_sources = [
        {
            "domain": d,
            "citation_count": c,
            "engines": sorted(per_domain_engines[d]),
            "top_intents": [i for i, _ in per_domain_intents[d].most_common(3)],
            "source_type": classify_source(f"https://{d}"),
            "is_owned": d in owned_domains,
        }
        for d, c in per_domain.most_common(25)
    ]

    return {
        "total_citations": total_citations,
        "unique_domains": len(per_domain),
        "source_type_counts": dict(type_counts),
        "winning_sources": winning_sources,
        # the money line for the client report:
        "owned_citation_share": round(
            sum(c for d, c in per_domain.items() if d in owned_domains) / total_citations, 3
        ) if total_citations else 0.0,
    }
