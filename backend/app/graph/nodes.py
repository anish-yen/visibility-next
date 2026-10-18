"""The nine nodes of the agentic loop, one per PLAN.md "Intended improvement
workflow" step.

Every node is a thin wrapper: the actual work stays in the existing modules
(crawler, answer_engines, citations, tech_checks, rewrite_engine) and in
loop_runner/audit_pipeline helpers, which are imported, never copied. If a
scoring or prompt rule needs to change it changes in one place and both the
legacy loop and this graph pick it up.

No node publishes anything. There is deliberately no code path here that
writes to a customer's website - step 7 records an approval decision and step
8 re-measures, and that is the whole extent of it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app import audit_store
from app.graph.state import (
    MAX_CYCLES,
    MAX_REVISIONS,
    PLATEAU_DELTA,
    TARGET_MENTION_RATE,
    LoopState,
    new_budget,
)
from app.graph.validation import validate_artifacts
from app.services import rewrite_engine
from app.services.answer_engines import run_engines
from app.services.citations import build_citation_map
from app.services.crawler import _fetch_html, crawl_site, distill_site_context
from app.services.gemini_client import GeminiError
from app.services.tech_checks import run_tech_checks

# Imported, not reimplemented. audit_pipeline is owned by another branch, so
# this module only ever reads from it.
from app.audit_pipeline import (  # noqa: F401 - re-exported for monkeypatching in tests
    _bucket_average_scores,
    _brand_label,
    _evaluate_prompt,
    _fallback_evaluation,
    _fallback_prompts,
    _generate_prompts_with_gemini,
)
from app.loop_runner import _evaluate_with_real_engines

__all__ = [
    "crawl_node",
    "freeze_prompts_node",
    "evaluate_node",
    "diagnose_node",
    "rewrite_node",
    "validate_node",
    "human_review_node",
    "retest_node",
    "stop_rule_node",
    "route_after_validate",
    "route_after_review",
    "route_after_stop_rule",
    "BudgetExceeded",
]


class BudgetExceeded(RuntimeError):
    """Raised when a node would exceed the call-count cap."""


def _budget(state: LoopState) -> dict[str, int]:
    return state.get("budget") or new_budget()


def _over_budget(budget: dict[str, int]) -> str | None:
    if budget["engine_calls"] >= budget["max_engine_calls"]:
        return (
            f"engine call cap reached ({budget['engine_calls']}/"
            f"{budget['max_engine_calls']})"
        )
    if budget["llm_calls"] >= budget["max_llm_calls"]:
        return f"llm call cap reached ({budget['llm_calls']}/{budget['max_llm_calls']})"
    return None


# --- step 1 -------------------------------------------------------------

async def crawl_node(state: LoopState) -> dict[str, Any]:
    """Crawl fresh every cycle: the client's site changes as fixes ship."""
    audit = audit_store.get(state["audit_id"])
    if not audit:
        raise RuntimeError(f"audit {state['audit_id']} not found")

    target_site = await crawl_site(audit.primary_domain)
    crawled = await asyncio.gather(
        *(crawl_site(d) for d in audit.competitor_domains), return_exceptions=True
    )
    competitor_sites = [s for s in crawled if isinstance(s, dict)]

    return {
        "target_domain": audit.primary_domain,
        "target_site": target_site,
        "competitor_sites": competitor_sites,
        "target_label": target_site.get("label") or _brand_label(audit.primary_domain),
        "competitor_labels": [
            s.get("label") or _brand_label(s.get("domain", "")) for s in competitor_sites
        ],
        "competitor_domains": [s.get("domain", "") for s in competitor_sites],
        "budget": _budget(state),
    }


# --- step 2 -------------------------------------------------------------

async def freeze_prompts_node(state: LoopState) -> dict[str, Any]:
    """Generate the prompt set once, then reuse it verbatim.

    Comparability is the whole point: if cycle 1 asks different questions than
    cycle 0, the measured lift is noise. An existing prompt_set is therefore
    passed through untouched.
    """
    existing = state.get("prompt_set")
    if existing:
        return {"prompt_set": existing, "prompts_source": state.get("prompts_source", "frozen")}

    budget = _budget(state)
    audit = audit_store.get(state["audit_id"])
    try:
        prompts = await _generate_prompts_with_gemini(
            audit, state["target_site"], state["competitor_sites"]
        )
        source = "gemini"
    except GeminiError:
        prompts = _fallback_prompts(audit, state["target_site"], state["competitor_sites"])
        source = "fallback"
    budget = {**budget, "llm_calls": budget["llm_calls"] + 1}

    return {"prompt_set": prompts, "prompts_source": source, "budget": budget}


# --- step 3 -------------------------------------------------------------

async def evaluate_node(state: LoopState) -> dict[str, Any]:
    """Ask the real answer engines each frozen prompt and score the answers."""
    budget = _budget(state)
    prompt_results: list[dict[str, Any]] = []
    all_answers: list[dict[str, Any]] = []
    engines = state.get("engines") or ()

    for prompt in state["prompt_set"]:
        if _over_budget(budget) is not None:
            # Stop spending rather than silently truncating a measurement:
            # a partial prompt set is recorded as such below.
            break
        answers = await run_engines(prompt["text"], engines=engines)
        budget = {**budget, "engine_calls": budget["engine_calls"] + max(1, len(answers))}
        all_answers.extend({**a.__dict__, "intent": prompt.get("intent")} for a in answers)

        result = _evaluate_with_real_engines(
            prompt,
            answers,
            state["target_label"],
            state["target_domain"],
            state.get("competitor_labels") or [],
            state.get("competitor_domains") or [],
        )
        if not result:  # every engine failed for this prompt
            try:
                result = await _evaluate_prompt(
                    prompt=prompt,
                    target_site=state["target_site"],
                    competitor_sites=state["competitor_sites"],
                )
                budget = {**budget, "llm_calls": budget["llm_calls"] + 1}
            except GeminiError:
                result = _fallback_evaluation(
                    prompt, state["target_site"], state["competitor_sites"]
                )
        prompt_results.append(result)

    mention_rate = (
        sum(1 for p in prompt_results if p.get("mentioned")) / len(prompt_results)
        if prompt_results
        else 0.0
    )
    return {
        "prompt_results": prompt_results,
        "all_answers": all_answers,
        "mention_rate": round(mention_rate, 3),
        "budget": budget,
    }


# --- step 4 -------------------------------------------------------------

async def diagnose_node(state: LoopState) -> dict[str, Any]:
    """Turn the raw signals into one structured diagnosis.

    The legacy loop left citations, tech checks and weak buckets as three
    loose fields; the rewrite step then had to re-derive what was actually
    wrong. Composing them here gives step 5 a single explicit input.
    """
    citation_map = build_citation_map(
        state["all_answers"],
        owned_domains={state["target_domain"], *(state.get("competitor_domains") or [])},
    )
    target_distilled = distill_site_context(state["target_site"])

    homepage_html = ""
    try:
        homepage_html = await _fetch_html(f"https://{state['target_domain']}")
    except Exception:  # noqa: BLE001 - a failed fetch only weakens the tech checks
        pass

    checks = await run_tech_checks(
        state["target_domain"],
        homepage_html,
        target_distilled,
        state["target_site"].get("page_type_counts", {}),
    )
    failed_checks = [c for c in checks if not c.get("passed")]
    weak_buckets = _bucket_average_scores(state["prompt_results"])

    diagnosis = {
        "mention_rate": state.get("mention_rate", 0.0),
        "weak_buckets": weak_buckets,
        "failed_checks": [c.get("check") for c in failed_checks],
        "owned_citation_share": citation_map.get("owned_citation_share"),
        "winning_sources": [
            s.get("domain") for s in (citation_map.get("winning_sources") or [])[:5]
        ],
        "absent_prompts": [
            p.get("text") for p in state["prompt_results"] if not p.get("mentioned")
        ][:8],
    }
    return {
        "citation_map": citation_map,
        "tech_checks": checks,
        "failed_checks": failed_checks,
        "weak_buckets": weak_buckets,
        "diagnosis": diagnosis,
    }


# --- step 5 -------------------------------------------------------------

async def rewrite_node(state: LoopState) -> dict[str, Any]:
    """Evidence-driven rewrite drafts for the worst gaps."""
    budget = _budget(state)
    weak_prompts = sorted(state["prompt_results"], key=lambda p: p.get("score", 0.0))[:8]
    winning_evidence = await rewrite_engine.gather_winning_evidence(
        state["citation_map"].get("winning_sources") or []
    )
    target_distilled = distill_site_context(state["target_site"])

    artifacts: list[dict[str, Any]] = []
    for artifact_type in rewrite_engine.pick_artifacts(
        state.get("weak_buckets") or {},
        state.get("failed_checks") or [],
        state["citation_map"],
    ):
        if _over_budget(budget) is not None:
            break
        artifacts.append(
            await rewrite_engine.generate_artifact(
                artifact_type,
                client_distilled=target_distilled,
                weak_prompts=weak_prompts,
                winning_evidence=winning_evidence,
                brand_label=state["target_label"],
            )
        )
        budget = {**budget, "llm_calls": budget["llm_calls"] + 1}

    return {"artifacts": artifacts, "budget": budget}


# --- step 6 -------------------------------------------------------------

async def validate_node(state: LoopState) -> dict[str, Any]:
    """Deterministic checks only - no prompts, no model calls."""
    verdicts, shippable = validate_artifacts(
        state.get("artifacts") or [],
        target_site=state["target_site"],
        brand_label=state.get("target_label", ""),
    )
    return {"validation": verdicts, "shippable_artifacts": shippable}


# --- step 7 -------------------------------------------------------------

async def human_review_node(state: LoopState) -> dict[str, Any]:
    """Real pause. The graph stops here until an owner responds.

    This node cannot change a website. It records a decision and nothing
    else, which is why "never edits a live site" is a structural property of
    the graph rather than a promise in a comment: there is no publish call in
    this package at all.

    The resume payload is expected to look like:
        {"decision": "approve"|"revise"|"reject",
         "reviewer": "<who>",
         "published": bool,      # did the owner actually ship the artifacts?
         "notes": "<optional>"}
    """
    from langgraph.types import interrupt

    response = interrupt(
        {
            "kind": "artifact_review",
            "audit_id": state["audit_id"],
            "cycle_number": state.get("cycle_number", 0),
            "mention_rate": state.get("mention_rate"),
            "diagnosis": state.get("diagnosis"),
            "artifacts": state.get("shippable_artifacts") or [],
            "validation": state.get("validation") or [],
            "instructions": (
                "Review the drafts. Approve only what you are willing to publish "
                "yourself; this system never edits your site. Set published=true "
                "only after the change is live and publicly retrievable, otherwise "
                "the re-test would measure the old page."
            ),
        }
    )

    payload = response if isinstance(response, dict) else {"decision": str(response)}
    decision = str(payload.get("decision", "reject")).strip().lower()
    approval = {
        "decision": decision,
        "reviewer": payload.get("reviewer"),
        "published": bool(payload.get("published", False)),
        "notes": payload.get("notes"),
    }
    revision_count = state.get("revision_count", 0) + (1 if decision == "revise" else 0)
    return {"approval": approval, "revision_count": revision_count}


# --- step 8 -------------------------------------------------------------

async def retest_node(state: LoopState) -> dict[str, Any]:
    """Independent re-test: the same frozen prompts, asked again.

    Independent in the sense that matters here - the engines are queried
    afresh with no approved artifact text injected into the prompt, so a
    score change has to come from the published page being read, not from us
    handing the model the answer.
    """
    budget = _budget(state)
    results: list[dict[str, Any]] = []
    engines = state.get("engines") or ()

    for prompt in state["prompt_set"]:
        if _over_budget(budget) is not None:
            break
        answers = await run_engines(prompt["text"], engines=engines)
        budget = {**budget, "engine_calls": budget["engine_calls"] + max(1, len(answers))}
        result = _evaluate_with_real_engines(
            prompt,
            answers,
            state["target_label"],
            state["target_domain"],
            state.get("competitor_labels") or [],
            state.get("competitor_domains") or [],
        )
        if result:
            results.append(result)

    rate = (
        sum(1 for p in results if p.get("mentioned")) / len(results) if results else 0.0
    )
    before = state.get("mention_rate", 0.0)
    return {
        "retest": {
            "prompt_count": len(results),
            "mention_rate": round(rate, 3),
            "delta_vs_pre_change": round(rate - before, 3),
            "measured_on_frozen_prompt_set": True,
        },
        "budget": budget,
    }


# --- step 9 -------------------------------------------------------------

async def stop_rule_node(state: LoopState) -> dict[str, Any]:
    """Target mention rate, plateau, max cycles, and the call-count cap."""
    budget = _budget(state)
    # Prefer the independently re-tested rate when we have one: it reflects
    # the published change rather than the pre-change measurement.
    retest = state.get("retest") or {}
    current = retest.get("mention_rate", state.get("mention_rate", 0.0))
    cycle_number = state.get("cycle_number", 0)

    previous_rate = state.get("previous_mention_rate")
    lift = None if previous_rate is None else round(current - previous_rate, 3)

    decision, reason = "continue", ""
    if (over := _over_budget(budget)) is not None:
        decision, reason = "stop", over
    elif current >= TARGET_MENTION_RATE:
        decision, reason = "stop", f"target mention rate {TARGET_MENTION_RATE} reached"
    elif cycle_number >= MAX_CYCLES:
        decision, reason = "stop", f"max cycles ({MAX_CYCLES}) reached"
    elif lift is not None and abs(lift) < PLATEAU_DELTA and cycle_number >= 1:
        previous_lift = state.get("previous_lift")
        if previous_lift is not None and abs(previous_lift) < PLATEAU_DELTA:
            decision, reason = (
                "stop",
                f"plateau: lift {lift} and {previous_lift} both under {PLATEAU_DELTA}",
            )

    approval = state.get("approval") or {}
    if decision == "continue" and approval.get("decision") == "reject":
        decision, reason = "stop", "owner rejected the drafts"
    if (
        decision == "continue"
        and approval.get("decision") == "approve"
        and not approval.get("published")
    ):
        decision, reason = (
            "stop",
            "approved but not published yet - nothing new to measure",
        )

    history = list(state.get("history") or [])
    history.append(
        {
            "cycle_number": cycle_number,
            "mention_rate": state.get("mention_rate"),
            "retest_mention_rate": retest.get("mention_rate"),
            "lift": lift,
            "decision": decision,
            "decision_reason": reason,
            "approval": approval.get("decision"),
            "engine_calls": budget["engine_calls"],
            "llm_calls": budget["llm_calls"],
        }
    )
    return {
        "lift": lift,
        "decision": decision,
        "decision_reason": reason,
        "history": history,
        "previous_mention_rate": current,
        "previous_lift": lift,
        "cycle_number": cycle_number + 1 if decision == "continue" else cycle_number,
    }


# --- routing ------------------------------------------------------------

def route_after_validate(state: LoopState) -> str:
    """No shippable draft means there is nothing worth a human's attention."""
    return "human_review" if state.get("shippable_artifacts") else "stop_rule"


def route_after_review(state: LoopState) -> str:
    approval = state.get("approval") or {}
    decision = approval.get("decision")
    if decision == "revise" and state.get("revision_count", 0) <= MAX_REVISIONS:
        return "rewrite"
    if decision == "approve" and approval.get("published"):
        return "retest"
    # Rejected, revision budget spent, or approved-but-unpublished: go
    # straight to the stop rule, which reports honestly that there was
    # nothing new to measure instead of re-running the same measurement and
    # calling it a result.
    return "stop_rule"


def route_after_stop_rule(state: LoopState) -> str:
    return "crawl" if state.get("decision") == "continue" else "__end__"
