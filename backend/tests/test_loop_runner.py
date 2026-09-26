"""run_cycle: full loop with fake engines/crawler, prompt reuse, lift, stopping rules."""
import asyncio

from app import audit_store, loop_runner
from app.cycle_store import InMemorySnapshotStore
from app.services.answer_engines import EngineAnswer

PROMPTS = [
    {"id": f"p{i}", "text": f"buyer question {i}", "intent": "best"}
    for i in range(4)
]


def _patch(monkeypatch, mentioned: set[str], gen_calls: list):
    async def fake_crawl(domain, **kw):
        return {"domain": domain, "label": domain.split(".")[0].title(),
                "page_type_counts": {}}

    async def fake_engines(prompt_text, *, engines=()):
        hit = prompt_text in mentioned
        return [EngineAnswer(
            engine="gemini",
            prompt_text=prompt_text,
            answer_text=("Acme is a solid pick, see acme.com" if hit
                         else "Try some incumbent instead."),
            cited_urls=(["https://acme.com/blog"] if hit else ["https://g2.com/x"]),
        )]

    async def fake_generate(state, target_site, competitor_sites):
        gen_calls.append(1)
        return [dict(p) for p in PROMPTS]

    async def fake_checks(domain, homepage_html, distilled, page_type_counts):
        return [{"id": "llms_txt", "passed": False, "detail": "missing"}]

    async def no_html(url):
        return ""

    monkeypatch.setattr(loop_runner, "crawl_site", fake_crawl)
    monkeypatch.setattr(loop_runner, "run_engines", fake_engines)
    monkeypatch.setattr(loop_runner, "_generate_prompts_with_gemini", fake_generate)
    monkeypatch.setattr(loop_runner, "run_tech_checks", fake_checks)
    monkeypatch.setattr(loop_runner, "_fetch_html", no_html)
    monkeypatch.setattr(loop_runner, "distill_site_context", lambda site: {})
    monkeypatch.setattr(loop_runner, "_bucket_average_scores", lambda results: {})
    monkeypatch.setattr(loop_runner.rewrite_engine, "gather_winning_evidence",
                        _async_return([]))
    monkeypatch.setattr(loop_runner.rewrite_engine, "pick_artifacts",
                        lambda *a, **kw: [])


def _async_return(value):
    async def _f(*a, **kw):
        return value
    return _f


def _mentions(*idxs):
    return {PROMPTS[i]["text"] for i in idxs}


def test_cycle0_full_snapshot(monkeypatch):
    gen_calls = []
    _patch(monkeypatch, _mentions(0), gen_calls)
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u1", "acme.com", ["rival.com"], "devtools")

    snap = asyncio.run(loop_runner.run_cycle(a.id, store))

    assert snap["cycle_number"] == 0
    assert snap["mention_rate"] == 0.25
    assert snap["lift"] is None
    assert snap["decision"] == "continue"
    assert snap["snapshot_id"]
    assert len(snap["prompt_results"]) == 4
    p0 = snap["prompt_results"][0]
    assert p0["mentioned"] is True
    assert p0["answers"][0]["engine"] == "gemini"
    assert p0["answers"][0]["cited_urls"] == ["https://acme.com/blog"]
    assert snap["citation_map"]["total_citations"] == 4
    # stored row carries lift/decision for the next cycle's plateau check
    stored = asyncio.run(store.latest(a.id))
    assert stored["decision"] == "continue"
    assert stored["prompt_set"][0]["text"] == PROMPTS[0]["text"]


def test_cycle1_reuses_prompts_and_computes_lift(monkeypatch):
    gen_calls = []
    _patch(monkeypatch, _mentions(0), gen_calls)
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u2", "acme.com", [], "devtools")
    asyncio.run(loop_runner.run_cycle(a.id, store))

    _patch(monkeypatch, _mentions(0, 1), gen_calls)
    snap = asyncio.run(loop_runner.run_cycle(a.id, store))

    assert len(gen_calls) == 1  # cycle 1 reused the cycle-0 prompt set
    assert snap["cycle_number"] == 1
    assert snap["mention_rate"] == 0.5
    assert snap["lift"] == 0.25
    assert snap["decision"] == "continue"
    assert [p["id"] for p in snap["prompt_set"]] == [p["id"] for p in PROMPTS]


def test_stops_at_target_mention_rate(monkeypatch):
    gen_calls = []
    _patch(monkeypatch, _mentions(0, 1, 2, 3), gen_calls)
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u3", "acme.com", [], None)
    snap = asyncio.run(loop_runner.run_cycle(a.id, store))
    assert snap["mention_rate"] == 1.0
    assert snap["decision"] == "stop"
    assert "target" in snap["decision_reason"]


def test_plateau_stop(monkeypatch):
    gen_calls = []
    _patch(monkeypatch, _mentions(0, 1), gen_calls)
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u4", "acme.com", [], None)
    # seed cycle 0: 0.5 mention rate, previous lift already flat
    asyncio.run(store.save({
        "audit_id": a.id, "cycle_number": 0, "mention_rate": 0.5, "lift": 0.01,
        "prompt_set": [dict(p) for p in PROMPTS],
        "prompt_results": [], "citation_map": {}, "tech_checks": [], "artifacts": [],
        "decision": "continue", "decision_reason": "",
    }))
    snap = asyncio.run(loop_runner.run_cycle(a.id, store))
    assert snap["cycle_number"] == 1
    assert snap["lift"] == 0.0
    assert snap["decision"] == "stop"
    assert "plateau" in snap["decision_reason"]


def test_max_cycles_stop(monkeypatch):
    gen_calls = []
    _patch(monkeypatch, _mentions(0), gen_calls)
    store = InMemorySnapshotStore()
    a = audit_store.create_audit("u5", "acme.com", [], None)
    asyncio.run(store.save({
        "audit_id": a.id, "cycle_number": loop_runner.MAX_CYCLES - 1,
        "mention_rate": 0.2, "lift": 0.2,
        "prompt_set": [dict(p) for p in PROMPTS],
        "prompt_results": [], "citation_map": {}, "tech_checks": [], "artifacts": [],
        "decision": "continue", "decision_reason": "",
    }))
    snap = asyncio.run(loop_runner.run_cycle(a.id, store))
    assert snap["cycle_number"] == loop_runner.MAX_CYCLES
    assert snap["decision"] == "stop"
    assert "max cycles" in snap["decision_reason"]


def test_unknown_audit_raises():
    store = InMemorySnapshotStore()
    try:
        asyncio.run(loop_runner.run_cycle("no-such-id", store))
    except RuntimeError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
