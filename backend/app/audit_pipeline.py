from __future__ import annotations

import asyncio
import random
import uuid

from app import audit_store


def _brand_label(domain: str) -> str:
    part = domain.split(".")[0]
    return part.replace("-", " ").title()


def _mock_results(state: audit_store.AuditState) -> None:
    random.seed(hash(state.id) % (2**32))
    brand = _brand_label(state.primary_domain)
    ind = state.industry or "your market"

    base = random.randint(38, 78)
    you = min(100, base + random.randint(-5, 12))
    comp_scores = [{"domain": state.primary_domain, "score": float(you), "label": "You"}]
    for d in state.competitor_domains:
        comp_scores.append(
            {
                "domain": d,
                "score": float(min(100, you + random.randint(-25, 15))),
                "label": _brand_label(d),
            }
        )

    prompt_templates = [
        (f"Best {ind} software for small teams", True),
        (f"{brand} vs alternatives for growing companies", random.choice([True, False])),
        (f"Is {brand} worth it for startups?", random.choice([True, True, False])),
        (f"How to choose a {ind} platform in 2026", True),
        (f"Affordable options similar to {brand}", False),
        (f"What do reviews say about {brand}?", random.choice([True, False])),
        (f"Enterprise {ind} tools compared", False),
        (f"Best free tier for {ind} products", random.choice([True, False])),
        (f"{brand} pricing and features explained", True),
        (f"Top rated vendors in {ind}", random.choice([True, False])),
    ]

    prompts = []
    for text, mentioned in prompt_templates:
        s = random.uniform(0.35, 0.95) if mentioned else random.uniform(0.1, 0.45)
        prompts.append(
            {
                "id": str(uuid.uuid4()),
                "text": text,
                "mentioned": mentioned,
                "score": round(s, 2),
            }
        )

    rec_specs = [
        (
            "Add a competitor comparison page",
            "Several comparative prompts under-index for your domain; a dedicated comparison page usually lifts simulated visibility.",
            0.91,
        ),
        (
            "Expand FAQ for buying objections",
            "Trust- and pricing-related prompts show weak mention strength; FAQs targeting objections help.",
            0.84,
        ),
        (
            "Publish vertical landing pages",
            f"Industry-specific queries ({ind}) are competitive; vertical pages improve topical match.",
            0.79,
        ),
        (
            "Surface customer proof (case studies)",
            "Review-oriented prompts rarely cite your brand; add concrete proof and logos.",
            0.72,
        ),
        (
            "Clarify positioning on the homepage",
            "Informational prompts show inconsistent entity recognition; tighten H1 and meta.",
            0.65,
        ),
    ]

    recommendations = []
    for i, (title, rationale, pri) in enumerate(rec_specs):
        recommendations.append(
            {
                "id": f"rec-{state.id[:8]}-{i}",
                "title": title,
                "rationale": rationale,
                "priority_score": round(pri - i * 0.02, 2),
                "brief": None,
            }
        )

    mention_scores = [p["score"] for p in prompts if p["mentioned"]]
    blended = (
        (sum(mention_scores) / len(mention_scores)) * 100 if mention_scores else float(you)
    )
    visibility = float(min(100, max(0, round(blended + random.uniform(-6, 6), 1))))

    audit_store.complete_audit(
        state.id,
        visibility_score=visibility,
        competitor_scores=comp_scores,
        prompts=prompts,
        recommendations=recommendations,
    )


async def run_simulated_audit(audit_id: str) -> None:
    """Advances stages and fills mock results (replace with real pipeline later)."""
    steps = [
        ("crawling", 22),
        ("generating_prompts", 48),
        ("evaluating", 74),
        ("analyzing", 92),
    ]
    try:
        for stage, pct in steps:
            await asyncio.sleep(1.6)
            audit_store.update_progress(audit_id, stage=stage, progress_percent=pct)
        state = audit_store.get(audit_id)
        if state:
            _mock_results(state)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        audit_store.fail_audit(audit_id, str(e))
