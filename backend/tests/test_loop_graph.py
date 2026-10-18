"""Plumbing tests for the agentic loop graph.

SCOPE WARNING, read before trusting these: every one of these tests is a
PLUMBING test. They assert state transitions, routing, stop rules, budget
enforcement, deterministic validation, and that the human review gate really
pauses and resumes. Every crawler, answer-engine and rewrite call is a fake.

They therefore measure NOTHING about answer quality, prompt quality, or
whether a visibility score is correct. No Gemini or other LLM call is made
here, by design. A green run means "the machine moves through the workflow in
the right order and stops when it should", not "the loop improves visibility".
That second claim needs a real run, which has not happened.
"""

import asyncio

import pytest

from app import audit_store
from app.graph import nodes as graph_nodes
from app.graph.state import MAX_ENGINE_CALLS, new_budget
from app.graph.validation import validate_artifact, validate_artifacts

pytest.importorskip("langgraph", reason="langgraph not installed in this environment")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.graph.loop_graph import (  # noqa: E402
    LOOP_ENGINE_ENV,
    build_graph,
    graph_engine_selected,
    initial_state,
)

TARGET_SITE = {
    "domain": "acme.com",
    "label": "Acme",
    "page_type_counts": {"homepage": 1},
    "pages": [{
        "url": "https://acme.com",
        "title": "Acme",
        "content_text": "Acme helps teams. Trusted by 40 teams. Plans from $20 per seat.",
        "headings": ["Why Acme"],
    }],
}
PROMPTS = [
    {"id": f"p{i}", "text": f"buyer question {i}", "intent": "informational"}
    for i in range(4)
]


class FakeAnswer:
    """Stand-in for answer_engines.EngineAnswer."""

    def __init__(self, text, cited, mentioned_domains):
        self.engine = "fake"
        self.prompt_text = "q"
        self.answer_text = text
        self.cited_urls = cited
        self.error = None
        self.mentioned_domains = mentioned_domains
        self.mentioned_labels = []


def _patch_graph(monkeypatch, *, mention_prompts=(), artifacts=None, record=None):
    """Replace every outbound call with a fake. Nothing here touches a network
    or an LLM."""
    calls = record if record is not None else []

    async def fake_crawl(domain, **kw):
        calls.append(("crawl", domain))
        return dict(TARGET_SITE, domain=domain, label=domain.split(".")[0].title())

    async def fake_run_engines(prompt_text, *, engines=()):
        calls.append(("engine", prompt_text))
        hit = prompt_text in mention_prompts
        return [FakeAnswer(
            "Acme is a good pick" if hit else "Try something else",
            ["https://acme.com/x"] if hit else ["https://g2.com/y"],
            ["acme.com"] if hit else [],
        )]

    def fake_evaluate(prompt, answers, t_label, t_domain, c_labels, c_domains):
        hit = prompt["text"] in mention_prompts
        return {
            "id": prompt["id"], "text": prompt["text"], "intent": prompt.get("intent"),
            "mentioned": hit, "score": 1.0 if hit else 0.0,
            "explanation": "fake", "competitor_mentions": [], "score_components": {},
            "answers": [],
        }

    async def fake_tech_checks(domain, html, distilled, counts):
        return [{"check": "llms_txt", "passed": False, "detail": "missing"}]

    async def no_html(url):
        return ""

    async def fake_generate_prompts(state, target, competitors):
        calls.append(("generate_prompts", None))
        return [dict(p) for p in PROMPTS]

    async def fake_gather(sources):
        return []

    async def fake_generate_artifact(artifact_type, **kw):
        calls.append(("artifact", artifact_type))
        return (artifacts or {}).get(artifact_type) or {
            "artifact_type": artifact_type, "status": "generated", "format": "markdown",
            "content": "Acme helps teams. Trusted by 40 teams.",
            "changes_vs_pattern": [], "placeholders": [], "grounded_in": [],
        }

    monkeypatch.setattr(graph_nodes, "crawl_site", fake_crawl)
    monkeypatch.setattr(graph_nodes, "run_engines", fake_run_engines)
    monkeypatch.setattr(graph_nodes, "_evaluate_with_real_engines", fake_evaluate)
    monkeypatch.setattr(graph_nodes, "run_tech_checks", fake_tech_checks)
    monkeypatch.setattr(graph_nodes, "_fetch_html", no_html)
    monkeypatch.setattr(graph_nodes, "distill_site_context", lambda s: {})
    monkeypatch.setattr(graph_nodes, "_generate_prompts_with_gemini", fake_generate_prompts)
    monkeypatch.setattr(graph_nodes, "_bucket_average_scores", lambda r: {"informational": 0.1})
    monkeypatch.setattr(graph_nodes, "build_citation_map", lambda a, owned_domains=None: {
        "total_citations": len(a), "owned_citation_share": 0.0, "winning_sources": [],
    })
    monkeypatch.setattr(graph_nodes.rewrite_engine, "gather_winning_evidence", fake_gather)
    monkeypatch.setattr(graph_nodes.rewrite_engine, "pick_artifacts", lambda *a, **k: ["faq_block"])
    monkeypatch.setattr(graph_nodes.rewrite_engine, "generate_artifact", fake_generate_artifact)
    return calls


def _run(graph, state, config):
    return asyncio.run(graph.ainvoke(state, config=config))


def _resume(graph, payload, config):
    return asyncio.run(graph.ainvoke(Command(resume=payload), config=config))


# --- flag / default behaviour -------------------------------------------

def test_legacy_engine_is_the_default(monkeypatch):
    monkeypatch.delenv(LOOP_ENGINE_ENV, raising=False)
    assert graph_engine_selected() is False


def test_graph_engine_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv(LOOP_ENGINE_ENV, "graph")
    assert graph_engine_selected() is True
    monkeypatch.setenv(LOOP_ENGINE_ENV, "legacy")
    assert graph_engine_selected() is False


def test_build_graph_refuses_without_a_checkpointer():
    with pytest.raises(ValueError, match="checkpointer"):
        build_graph(None)


# --- the human review gate is a real pause ------------------------------

def test_graph_pauses_at_human_review_and_does_not_continue(monkeypatch):
    calls = _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u1", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}

    out = _run(graph, initial_state(audit.id), config)

    assert "__interrupt__" in out, "graph must stop at the review gate"
    payload = out["__interrupt__"][0].value
    assert payload["kind"] == "artifact_review"
    assert payload["audit_id"] == audit.id
    assert payload["artifacts"], "owner needs the drafts to review"
    # Steps after the gate must not have run.
    assert "retest" not in out
    assert out.get("decision") is None


def test_resume_with_approval_and_published_runs_the_independent_retest(monkeypatch):
    calls = _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u2", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}
    _run(graph, initial_state(audit.id), config)

    engine_calls_before = sum(1 for c in calls if c[0] == "engine")
    out = _resume(graph, {"decision": "approve", "reviewer": "anish", "published": True}, config)

    assert out["approval"]["decision"] == "approve"
    assert out["retest"]["measured_on_frozen_prompt_set"] is True
    assert out["retest"]["prompt_count"] == len(PROMPTS)
    # A re-test must actually re-query, not reuse the first measurement.
    assert sum(1 for c in calls if c[0] == "engine") > engine_calls_before


def test_approved_but_not_published_stops_without_claiming_a_retest(monkeypatch):
    _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u3", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}
    _run(graph, initial_state(audit.id), config)

    out = _resume(graph, {"decision": "approve", "published": False}, config)

    assert out["decision"] == "stop"
    assert "not published" in out["decision_reason"]
    assert "retest" not in out, "must not report a measurement it did not take"


def test_rejection_stops_the_loop(monkeypatch):
    _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u4", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}
    _run(graph, initial_state(audit.id), config)

    out = _resume(graph, {"decision": "reject", "reviewer": "anish"}, config)

    assert out["decision"] == "stop"
    assert "rejected" in out["decision_reason"]
    assert "retest" not in out


def test_revise_loops_back_to_rewrite_and_pauses_again(monkeypatch):
    calls = _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u5", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}
    _run(graph, initial_state(audit.id), config)
    artifacts_before = sum(1 for c in calls if c[0] == "artifact")

    out = _resume(graph, {"decision": "revise", "notes": "too generic"}, config)

    assert sum(1 for c in calls if c[0] == "artifact") > artifacts_before
    assert "__interrupt__" in out, "a revision must come back for review"


def test_graph_package_contains_no_outbound_write_calls():
    """Structural guarantee rather than a promise in a docstring.

    "Never edits a live site" holds because the graph package has no way to
    write anywhere: no HTTP write verb, no publish/deploy helper. Checked on
    code with comments and strings stripped, so prose about publishing does
    not satisfy or trip the assertion.
    """
    import ast
    import pathlib

    banned_methods = {"post", "put", "patch", "delete"}
    banned_names = {"publish", "deploy", "push_to_site", "update_live", "write_page"}

    pkg = pathlib.Path(graph_nodes.__file__).parent
    offenders: list[str] = []
    checked = 0
    for path in sorted(pkg.glob("*.py")):
        checked += 1
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr.lower() in banned_methods:
                offenders.append(f"{path.name}:{node.lineno} .{func.attr}()")
            if isinstance(func, ast.Name) and func.id.lower() in banned_names:
                offenders.append(f"{path.name}:{node.lineno} {func.id}()")

    assert checked >= 5, "expected to scan the whole graph package"
    assert not offenders, f"outbound write call(s) in the graph package: {offenders}"


# --- frozen prompt set --------------------------------------------------

def test_prompt_set_is_generated_once_and_then_reused(monkeypatch):
    calls = _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]})
    audit = audit_store.create_audit("u6", "acme.com", [], None)

    first = asyncio.run(graph_nodes.freeze_prompts_node(
        {"audit_id": audit.id, "target_site": TARGET_SITE, "competitor_sites": [],
         "budget": new_budget()}
    ))
    assert len(calls) == 1 and first["prompts_source"] == "gemini"

    second = asyncio.run(graph_nodes.freeze_prompts_node(
        {"audit_id": audit.id, "target_site": TARGET_SITE, "competitor_sites": [],
         "prompt_set": first["prompt_set"], "budget": new_budget()}
    ))
    assert second["prompt_set"] == first["prompt_set"]
    assert len(calls) == 1, "a reused set must not call generation again"


# --- stop rules ---------------------------------------------------------

def _stop_state(**over):
    base = {
        "audit_id": "a", "cycle_number": 0, "mention_rate": 0.1,
        "budget": new_budget(), "history": [],
        "previous_mention_rate": None, "previous_lift": None,
        "approval": {"decision": "approve", "published": True},
    }
    base.update(over)
    return base


def test_stop_rule_target_mention_rate():
    out = asyncio.run(graph_nodes.stop_rule_node(_stop_state(mention_rate=0.9)))
    assert out["decision"] == "stop" and "target mention rate" in out["decision_reason"]


def test_stop_rule_max_cycles():
    from app.graph.state import MAX_CYCLES

    out = asyncio.run(graph_nodes.stop_rule_node(_stop_state(cycle_number=MAX_CYCLES)))
    assert out["decision"] == "stop" and "max cycles" in out["decision_reason"]


def test_stop_rule_plateau_needs_two_flat_cycles():
    one_flat = asyncio.run(graph_nodes.stop_rule_node(_stop_state(
        cycle_number=1, mention_rate=0.30, previous_mention_rate=0.29, previous_lift=None,
    )))
    assert one_flat["decision"] == "continue", "a single flat cycle is not a plateau"

    two_flat = asyncio.run(graph_nodes.stop_rule_node(_stop_state(
        cycle_number=2, mention_rate=0.30, previous_mention_rate=0.29, previous_lift=0.01,
    )))
    assert two_flat["decision"] == "stop" and "plateau" in two_flat["decision_reason"]


def test_stop_rule_budget_cap_is_enforced():
    spent = {**new_budget(), "engine_calls": MAX_ENGINE_CALLS}
    out = asyncio.run(graph_nodes.stop_rule_node(_stop_state(budget=spent)))
    assert out["decision"] == "stop" and "engine call cap" in out["decision_reason"]


def test_stop_rule_prefers_the_retested_rate_over_the_pre_change_one():
    out = asyncio.run(graph_nodes.stop_rule_node(_stop_state(
        mention_rate=0.10, retest={"mention_rate": 0.95},
    )))
    assert out["decision"] == "stop" and "target mention rate" in out["decision_reason"]


def test_budget_cap_stops_spending_mid_evaluation(monkeypatch):
    calls = _patch_graph(monkeypatch)
    nearly_spent = {**new_budget(), "engine_calls": MAX_ENGINE_CALLS - 1}
    out = asyncio.run(graph_nodes.evaluate_node({
        "audit_id": "a", "prompt_set": [dict(p) for p in PROMPTS],
        "target_site": TARGET_SITE, "competitor_sites": [],
        "target_label": "Acme", "target_domain": "acme.com",
        "competitor_labels": [], "competitor_domains": [],
        "budget": nearly_spent, "engines": (),
    }))
    # One call fits inside the cap, the rest must be refused.
    assert len(out["prompt_results"]) < len(PROMPTS)
    assert sum(1 for c in calls if c[0] == "engine") < len(PROMPTS)


# --- deterministic validation (step 6) ----------------------------------

def test_validation_passes_a_clean_artifact():
    verdict = validate_artifact(
        {"artifact_type": "faq_block", "status": "generated",
         "content": "Acme helps teams. Trusted by 40 teams.", "placeholders": []},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert verdict["passed"] is True and verdict["failures"] == []


def test_validation_rejects_invented_numbers():
    """The rewrite engine's hard rule is that unverified facts become
    placeholders, so a number that is nowhere in the crawl is a fabrication."""
    verdict = validate_artifact(
        {"artifact_type": "proof_section", "status": "generated",
         "content": "Acme is trusted by 9,000 teams and cuts costs 73%.",
         "placeholders": []},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert verdict["passed"] is False
    assert any("numeric claim" in f for f in verdict["failures"])


def test_validation_allows_numbers_that_are_in_the_crawled_facts():
    verdict = validate_artifact(
        {"artifact_type": "pricing_clarity_section", "status": "generated",
         "content": "Plans from $20 per seat, trusted by 40 teams.", "placeholders": []},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert verdict["passed"] is True, verdict["failures"]


def test_validation_allows_unverified_numbers_inside_confirm_placeholders():
    verdict = validate_artifact(
        {"artifact_type": "proof_section", "status": "generated",
         "content": "Acme serves [CONFIRM: 9,000 teams - check with sales].",
         "placeholders": ["[CONFIRM: 9,000 teams - check with sales]"]},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert verdict["passed"] is True, verdict["failures"]


def test_validation_rejects_failed_generation_and_template_leftovers():
    failed = validate_artifact(
        {"artifact_type": "faq_block", "status": "failed", "error": "HTTP 429"},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert failed["passed"] is False

    leftover = validate_artifact(
        {"artifact_type": "faq_block", "status": "generated",
         "content": "TODO: write the Acme FAQ", "placeholders": []},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert leftover["passed"] is False
    assert any("leftover" in f for f in leftover["failures"])


def test_validation_rejects_unterminated_placeholder():
    verdict = validate_artifact(
        {"artifact_type": "faq_block", "status": "generated",
         "content": "Acme is used by [CONFIRM: how many teams", "placeholders": []},
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert verdict["passed"] is False
    assert any("unterminated" in f for f in verdict["failures"])


def test_validate_artifacts_splits_shippable_from_rejected():
    verdicts, shippable = validate_artifacts(
        [
            {"artifact_type": "ok", "status": "generated",
             "content": "Acme helps teams.", "placeholders": []},
            {"artifact_type": "bad", "status": "generated",
             "content": "Acme grew 500% last year.", "placeholders": []},
        ],
        target_site=TARGET_SITE, brand_label="Acme",
    )
    assert [v["passed"] for v in verdicts] == [True, False]
    assert [a["artifact_type"] for a in shippable] == ["ok"]


def test_no_shippable_artifact_skips_the_human_gate(monkeypatch):
    """Nothing passed validation, so there is nothing worth a human's time."""
    _patch_graph(monkeypatch, mention_prompts={PROMPTS[0]["text"]}, artifacts={
        "faq_block": {"artifact_type": "faq_block", "status": "generated",
                      "content": "Acme grew 500% last year.", "placeholders": []},
    })
    audit = audit_store.create_audit("u7", "acme.com", [], None)
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": audit.id}}

    out = _run(graph, initial_state(audit.id), config)

    assert "__interrupt__" not in out
    assert out["decision"] in {"stop", "continue"}
    assert out["validation"][0]["passed"] is False
