"""The 9-node agentic loop as a LangGraph state graph.

Maps 1:1 onto PLAN.md's "Intended improvement workflow":

    1 crawl -> 2 freeze_prompts -> 3 evaluate -> 4 diagnose -> 5 rewrite
      -> 6 validate -> 7 human_review (interrupt) -> 8 retest -> 9 stop_rule

Edges that are not a straight line:
  - validate -> stop_rule when no draft passed validation (nothing worth a
    human's time)
  - human_review -> rewrite on "revise" (bounded by MAX_REVISIONS)
  - human_review -> stop_rule on reject, or on approve-without-publish
  - stop_rule -> crawl while the decision is "continue", else END

This lives BESIDE loop_runner.run_cycle and does not replace it. The legacy
loop stays the default; set ZETEUM_LOOP_ENGINE=graph to select this one.
"""

from __future__ import annotations

import os
from typing import Any

from app.graph.nodes import (
    crawl_node,
    diagnose_node,
    evaluate_node,
    freeze_prompts_node,
    human_review_node,
    retest_node,
    rewrite_node,
    route_after_review,
    route_after_stop_rule,
    route_after_validate,
    stop_rule_node,
    validate_node,
)
from app.graph.state import LoopState, new_budget

__all__ = [
    "LOOP_ENGINE_ENV",
    "build_graph",
    "graph_engine_selected",
    "initial_state",
    "run_graph_cycle",
]

LOOP_ENGINE_ENV = "ZETEUM_LOOP_ENGINE"


def graph_engine_selected() -> bool:
    """True only when explicitly opted in. Default stays the legacy loop."""
    return os.environ.get(LOOP_ENGINE_ENV, "legacy").strip().lower() == "graph"


def build_graph(checkpointer: Any):
    """Compile the graph. A checkpointer is REQUIRED: without one the
    human_review interrupt cannot pause and resume at all."""
    if checkpointer is None:
        raise ValueError(
            "build_graph requires a checkpointer; the human review gate "
            "interrupts the graph and needs one to resume."
        )

    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(LoopState)
    builder.add_node("crawl", crawl_node)
    builder.add_node("freeze_prompts", freeze_prompts_node)
    builder.add_node("evaluate", evaluate_node)
    builder.add_node("diagnose", diagnose_node)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("validate", validate_node)
    builder.add_node("human_review", human_review_node)
    builder.add_node("retest", retest_node)
    builder.add_node("stop_rule", stop_rule_node)

    builder.add_edge(START, "crawl")
    builder.add_edge("crawl", "freeze_prompts")
    builder.add_edge("freeze_prompts", "evaluate")
    builder.add_edge("evaluate", "diagnose")
    builder.add_edge("diagnose", "rewrite")
    builder.add_edge("rewrite", "validate")

    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {"human_review": "human_review", "stop_rule": "stop_rule"},
    )
    builder.add_conditional_edges(
        "human_review",
        route_after_review,
        {"rewrite": "rewrite", "retest": "retest", "stop_rule": "stop_rule"},
    )
    builder.add_edge("retest", "stop_rule")
    builder.add_conditional_edges(
        "stop_rule",
        route_after_stop_rule,
        {"crawl": "crawl", "__end__": END},
    )

    return builder.compile(checkpointer=checkpointer)


def initial_state(audit_id: str, *, engines: tuple[str, ...] = ()) -> LoopState:
    return {
        "audit_id": audit_id,
        "engines": engines,
        "cycle_number": 0,
        "revision_count": 0,
        "budget": new_budget(),
        "history": [],
        "previous_mention_rate": None,
        "previous_lift": None,
    }


async def run_graph_cycle(
    audit_id: str,
    *,
    engines: tuple[str, ...] = (),
    checkpointer: Any = None,
) -> dict[str, Any]:
    """Run the graph until it ends or pauses at the human review gate.

    With no checkpointer passed, the durable Postgres one is built from
    DATABASE_URL and will raise CheckpointerUnavailable if it is unset -
    deliberately, rather than quietly running without durability.

    Returns the state LangGraph has reached. If it is waiting for review the
    returned dict carries LangGraph's `__interrupt__` payload; resume with
    `Command(resume={...})` on the same thread id (the audit id).
    """
    config = {"configurable": {"thread_id": audit_id}}
    state = initial_state(audit_id, engines=engines)

    if checkpointer is not None:
        graph = build_graph(checkpointer)
        return await graph.ainvoke(state, config=config)

    from app.graph.checkpointer import build_checkpointer

    async with build_checkpointer() as saver:
        await saver.setup()
        graph = build_graph(saver)
        return await graph.ainvoke(state, config=config)
