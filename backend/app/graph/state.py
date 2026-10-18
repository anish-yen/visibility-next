"""State carried through the agentic loop graph.

One flat TypedDict rather than nested models: LangGraph merges node return
values into this dict key-by-key, so a flat shape makes it obvious which node
owns which key. The "owned by" comments below are the contract - two nodes
writing the same key would make the graph's behaviour order-dependent.
"""

from __future__ import annotations

from typing import Any, TypedDict

# Stop rules. The first three are re-exported from loop_runner rather than
# redefined, so the legacy loop and the graph can never drift apart on what
# "done" means. The budget cap is new and graph-only.
from app.loop_runner import MAX_CYCLES, PLATEAU_DELTA, TARGET_MENTION_RATE

__all__ = [
    "LoopState",
    "MAX_CYCLES",
    "PLATEAU_DELTA",
    "TARGET_MENTION_RATE",
    "MAX_ENGINE_CALLS",
    "MAX_LLM_CALLS",
    "MAX_REVISIONS",
    "MAX_UNRESOLVED_PLACEHOLDERS",
    "new_budget",
]

# Budget is a call count, not a dollar figure: this repo has no cost table and
# inventing per-call prices would produce a confident number with no basis.
MAX_ENGINE_CALLS = 120   # answer-engine queries per graph run
MAX_LLM_CALLS = 40       # prompt generation + artifact generation per graph run

MAX_REVISIONS = 2        # bounded rewrite<-human_review loops
MAX_UNRESOLVED_PLACEHOLDERS = 6  # above this an artifact is not shippable


def new_budget() -> dict[str, int]:
    return {
        "engine_calls": 0,
        "llm_calls": 0,
        "max_engine_calls": MAX_ENGINE_CALLS,
        "max_llm_calls": MAX_LLM_CALLS,
    }


class LoopState(TypedDict, total=False):
    # --- inputs ---
    audit_id: str
    engines: tuple[str, ...]
    cycle_number: int

    # --- step 1: crawl ---
    target_domain: str
    target_site: dict[str, Any]
    competitor_sites: list[dict[str, Any]]
    target_label: str
    competitor_labels: list[str]
    competitor_domains: list[str]

    # --- step 2: frozen prompt set ---
    # Frozen so cycle N and N+1 are comparable; re-generating would make any
    # measured lift meaningless.
    prompt_set: list[dict[str, Any]]
    prompts_source: str

    # --- step 3: evaluate against real engines ---
    prompt_results: list[dict[str, Any]]
    all_answers: list[dict[str, Any]]
    mention_rate: float

    # --- step 4: diagnose ---
    citation_map: dict[str, Any]
    tech_checks: list[dict[str, Any]]
    failed_checks: list[dict[str, Any]]
    weak_buckets: dict[str, float]
    diagnosis: dict[str, Any]

    # --- step 5: rewrite ---
    artifacts: list[dict[str, Any]]

    # --- step 6: validate (deterministic, no LLM) ---
    validation: list[dict[str, Any]]
    shippable_artifacts: list[dict[str, Any]]

    # --- step 7: human review gate ---
    approval: dict[str, Any]
    revision_count: int

    # --- step 8: independent re-test ---
    retest: dict[str, Any]

    # --- step 9: stop rule ---
    lift: float | None
    decision: str
    decision_reason: str

    # --- bookkeeping ---
    budget: dict[str, int]
    history: list[dict[str, Any]]
    previous_mention_rate: float | None
    previous_lift: float | None
