"""The continuous visibility loop.

The current run_audit() is one pass: crawl, prompts, simulated eval, score,
recommendations. This module turns it into an iteration loop with a stopping
rule:

    cycle N:   crawl -> prompts (stable set) -> REAL engines -> citation map
               -> tech checks -> evidence-driven rewrites -> persist snapshot
    ship:      founder (or the agent, in the service product) deploys artifacts
    cycle N+1: re-run the SAME prompts -> diff mention rate & citation share
    stop when: lift plateaus (two cycles < 3 pts) or target mention rate hit

The stable prompt set is what makes cycles comparable. Prompts are generated
once (cycle 0) and reused; new prompts only get added when the gap analysis
surfaces an intent bucket with no coverage.

Persistence: audit_store is an in-memory dict, so snapshots die on restart.
Cycle snapshots must go to Supabase (see visibility_cycles.sql). The runner
below takes a `snapshot_store` duck-type with save()/latest() so it works with
the in-memory store today and the Supabase table tomorrow.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app import audit_store
from app.audit_pipeline import (
    _brand_label,
    _bucket_average_scores,
    _generate_prompts_with_gemini,
    _fallback_prompts,
    _evaluate_prompt,          # simulated fallback, kept for engine outages
    _fallback_evaluation,
)
from app.services.crawler import crawl_site, distill_site_context, _fetch_html
from app.services.gemini_client import GeminiError
from app.services.answer_engines import run_engines, detect_mentions, EngineAnswer, DEFAULT_ENGINES
from app.services.citations import build_citation_map
from app.services.tech_checks import run_tech_checks
from app.services import rewrite_engine

# --- stopping rule knobs ---
TARGET_MENTION_RATE = 0.60      # per-client goal, tune per niche
PLATEAU_DELTA = 0.03            # < 3 points of lift across two cycles = plateau
MAX_CYCLES = 6                  # hard cap so the loop always terminates


class SnapshotStore(Protocol):
    async def save(self, snapshot: dict[str, Any]) -> str: ...
    async def latest(self, audit_id: str) -> dict[str, Any] | None: ...


def _evaluate_with_real_engines(
    prompt: dict[str, Any],
    answers: list[EngineAnswer],
    target_label: str,
    target_domain: str,
    competitor_labels: list[str],
    competitor_domains: list[str],
) -> dict[str, Any]:
    """Score one prompt from real engine answers (replaces simulated grading).
    Mention = brand named in the answer text or its domain in citations."""
    labels = [target_label, *competitor_labels]
    domains = [target_domain, *competitor_domains]
    usable = [a for a in answers if not a.error]
    for a in usable:
        detect_mentions(a, labels, domains)

    if not usable:
        return {}
    mentioned = sum(1 for a in usable if target_domain in a.mentioned_domains)
    comp_mentioned = sorted({
        d for a in usable for d in a.mentioned_domains if d != target_domain
    })
    mention_rate = mentioned / len(usable)
    # score keeps the 0..1 shape the rest of the pipeline expects
    score = round(mention_rate, 2)
    return {
        "id": prompt["id"],
        "text": prompt["text"],
        "intent": prompt.get("intent"),
        "mentioned": mentioned > 0,
        "score": score,
        "explanation": (
            f"Real engines: mentioned in {mentioned}/{len(usable)} answers "
            f"({', '.join(a.engine for a in usable)})."
        ),
        "competitor_mentions": comp_mentioned,
        "score_components": {
            "engine_mention_rate": mention_rate,
            "engines_used": [a.engine for a in usable],
            "target_role": "central" if mention_rate >= 0.66 else "supporting" if mention_rate > 0 else "absent",
        },
        "answers": [
            {"engine": a.engine, "answer_text": a.answer_text[:2000], "cited_urls": a.cited_urls}
            for a in usable
        ],
    }


async def run_cycle(
    audit_id: str,
    snapshot_store: SnapshotStore,
    *,
    engines: tuple[str, ...] = DEFAULT_ENGINES,
) -> dict[str, Any]:
    state = audit_store.get(audit_id)
    if not state:
        raise RuntimeError(f"audit {audit_id} not found")

    previous = await snapshot_store.latest(audit_id)
    cycle_number = (previous["cycle_number"] + 1) if previous else 0

    # 1. crawl (fresh every cycle: the client's site changes as fixes ship)
    target_site = await crawl_site(state.primary_domain)
    competitor_sites = await asyncio.gather(
        *(crawl_site(d) for d in state.competitor_domains), return_exceptions=True,
    )
    competitor_sites = [s for s in competitor_sites if isinstance(s, dict)]

    # 2. prompt set: reuse the previous cycle's prompts for comparability;
    #    generate only on cycle 0
    if previous and previous.get("prompt_set"):
        prompts = previous["prompt_set"]
    else:
        try:
            prompts = await _generate_prompts_with_gemini(state, target_site, competitor_sites)
        except GeminiError:
            prompts = _fallback_prompts(state, target_site, competitor_sites)

    target_label = target_site.get("label") or _brand_label(state.primary_domain)
    comp_labels = [s.get("label") or _brand_label(s.get("domain", "")) for s in competitor_sites]
    comp_domains = [s.get("domain", "") for s in competitor_sites]

    # 3. run every prompt against the REAL engines (fall back per-prompt to
    #    the existing simulated evaluation if every engine is down)
    prompt_results: list[dict[str, Any]] = []
    all_answers: list[dict[str, Any]] = []
    for prompt in prompts:
        answers = await run_engines(prompt["text"], engines=engines)
        all_answers.extend(
            {**a.__dict__, "intent": prompt.get("intent")} for a in answers
        )
        result = _evaluate_with_real_engines(
            prompt, answers, target_label, state.primary_domain, comp_labels, comp_domains,
        )
        if not result:  # all engines failed
            try:
                result = await _evaluate_prompt(
                    prompt=prompt, target_site=target_site, competitor_sites=competitor_sites,
                )
            except GeminiError:
                result = _fallback_evaluation(prompt, target_site, competitor_sites)
        prompt_results.append(result)

    # 4. citation map: who the engines read for this category
    citation_map = build_citation_map(
        all_answers, owned_domains={state.primary_domain, *comp_domains},
    )

    # 5. technical gap analysis
    target_distilled = distill_site_context(target_site)
    homepage_html = ""
    try:
        homepage_html = await _fetch_html(f"https://{state.primary_domain}")
    except Exception:  # noqa: BLE001
        pass
    checks = await run_tech_checks(
        state.primary_domain, homepage_html, target_distilled,
        target_site.get("page_type_counts", {}),
    )
    failed_checks = [c for c in checks if not c["passed"]]

    # 6. evidence-driven rewrites for the worst gaps
    weak_buckets = _bucket_average_scores(prompt_results)
    weak_prompts = sorted(prompt_results, key=lambda p: p.get("score", 0.0))[:8]
    winning_evidence = await rewrite_engine.gather_winning_evidence(
        citation_map["winning_sources"]
    )
    artifacts = []
    for artifact_type in rewrite_engine.pick_artifacts(weak_buckets, failed_checks, citation_map):
        artifacts.append(await rewrite_engine.generate_artifact(
            artifact_type,
            client_distilled=target_distilled,
            weak_prompts=weak_prompts,
            winning_evidence=winning_evidence,
            brand_label=target_label,
        ))

    # 7. snapshot + lift vs previous cycle
    mention_rate = (
        sum(1 for p in prompt_results if p.get("mentioned")) / len(prompt_results)
        if prompt_results else 0.0
    )
    snapshot = {
        "audit_id": audit_id,
        "cycle_number": cycle_number,
        "prompt_set": prompts,
        "prompt_results": prompt_results,
        "mention_rate": round(mention_rate, 3),
        "citation_map": citation_map,
        "tech_checks": checks,
        "artifacts": artifacts,
    }
    snapshot_id = await snapshot_store.save(snapshot)

    lift = None
    if previous:
        lift = round(mention_rate - previous["mention_rate"], 3)

    # 8. stopping rule
    decision = "continue"
    reason = ""
    if mention_rate >= TARGET_MENTION_RATE:
        decision, reason = "stop", f"target mention rate {TARGET_MENTION_RATE} reached"
    elif cycle_number >= MAX_CYCLES:
        decision, reason = "stop", f"max cycles ({MAX_CYCLES}) reached"
    elif previous and lift is not None and abs(lift) < PLATEAU_DELTA and cycle_number >= 1:
        prev_lift = previous.get("lift")
        if prev_lift is not None and abs(prev_lift) < PLATEAU_DELTA:
            decision, reason = "stop", f"plateau: lift {lift} and {prev_lift} both under {PLATEAU_DELTA}"
    snapshot["lift"] = lift
    snapshot["decision"] = decision
    snapshot["decision_reason"] = reason
    snapshot["snapshot_id"] = snapshot_id
    return snapshot
