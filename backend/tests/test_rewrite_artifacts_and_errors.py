"""Two unrelated additions, same change:

1. Rewrite engine wired into the ordinary (non-cycle) audit report: the
   weakest prompt buckets get an evidence-grounded rewrite draft attached to
   the audit, reusing competitor pages already crawled this run instead of a
   second live fetch. Gemini is quota-blocked in this environment, so every
   test here mocks rewrite_engine.generate_artifact / the Gemini call sites
   directly rather than hitting the real API.
2. A real 429 from Gemini now surfaces as a distinct "quota" error_type on
   the failed audit (vs. "generic" for any other Gemini failure), so the
   dashboard can show a plain retry-later banner instead of a generic one.
"""
import asyncio

import httpx
import pytest

from app import audit_pipeline, audit_store
from app.config import get_settings
from app.services import gemini_client as gemini_client_mod
from app.services.gemini_client import GeminiClient, GeminiError


def _artifact_stub(artifact_type: str, **overrides):
    base = {
        "artifact_type": artifact_type,
        "status": "generated",
        "format": "markdown",
        "content": "Draft content.",
        "changes_vs_pattern": [],
        "placeholders": [],
        "grounded_in": [],
    }
    base.update(overrides)
    return base


# --- rewrite engine wiring ---------------------------------------------

def test_generate_rewrite_artifacts_skips_when_nothing_is_weak():
    state = audit_store.create_audit("u1", "acme.com", [], None)
    result = asyncio.run(audit_pipeline._generate_rewrite_artifacts(
        state, {"domain": "acme.com"}, [], [], {"informational": 0.9, "pricing": 0.8},
    ))
    assert result == []


def test_generate_rewrite_artifacts_targets_weakest_buckets_first(monkeypatch):
    calls = []

    async def fake_generate_artifact(artifact_type, *, client_distilled, weak_prompts, winning_evidence, brand_label):
        calls.append((artifact_type, [p["text"] for p in weak_prompts], brand_label))
        return _artifact_stub(
            artifact_type,
            content="Draft with a [CONFIRM: customer count] placeholder.",
            placeholders=["[CONFIRM: customer count]"],
        )

    monkeypatch.setattr(audit_pipeline.rewrite_engine, "generate_artifact", fake_generate_artifact)

    state = audit_store.create_audit("u2", "acme.com", [], None)
    target_site = {"domain": "acme.com", "label": "Acme"}
    prompts = [
        {"id": "p1", "text": "is acme cheaper than rival pricing", "score": 0.1},
        {"id": "p2", "text": "acme reviews trusted", "score": 0.95},
        {"id": "p3", "text": "what does acme do", "score": 0.2},
    ]
    weak_buckets = {"pricing": 0.1, "informational": 0.2, "trust": 0.95}

    artifacts = asyncio.run(audit_pipeline._generate_rewrite_artifacts(
        state, target_site, [], prompts, weak_buckets,
    ))

    assert [a["artifact_type"] for a in artifacts] == ["pricing_clarity_section", "faq_block"]
    assert [a["bucket"] for a in artifacts] == ["pricing", "informational"]
    assert artifacts[0]["bucket_score"] == 0.1
    assert "[CONFIRM: customer count]" in artifacts[0]["content"]
    assert calls[0][2] == "Acme"
    # pricing bucket's own weak prompt was selected, not the whole set
    assert calls[0][1] == ["is acme cheaper than rival pricing"]


def test_generate_rewrite_artifacts_caps_at_max(monkeypatch):
    async def fake_generate_artifact(artifact_type, **kw):
        return _artifact_stub(artifact_type)

    monkeypatch.setattr(audit_pipeline.rewrite_engine, "generate_artifact", fake_generate_artifact)
    state = audit_store.create_audit("u3", "acme.com", [], None)
    weak_buckets = {"informational": 0.1, "pricing": 0.2, "trust": 0.3, "comparative": 0.4}
    prompts = [{"id": "p1", "text": "what is acme", "score": 0.1}]

    artifacts = asyncio.run(audit_pipeline._generate_rewrite_artifacts(
        state, {"domain": "acme.com"}, [], prompts, weak_buckets,
    ))

    assert len(artifacts) == audit_pipeline.MAX_REWRITE_ARTIFACTS
    assert [a["bucket"] for a in artifacts] == ["informational", "pricing"]


def test_generate_rewrite_artifacts_passes_through_failed_status(monkeypatch):
    async def fake_generate_artifact(artifact_type, **kw):
        return {"artifact_type": artifact_type, "status": "failed",
                "error": "Gemini request failed: HTTP 429: quota exceeded"}

    monkeypatch.setattr(audit_pipeline.rewrite_engine, "generate_artifact", fake_generate_artifact)
    state = audit_store.create_audit("u4", "acme.com", [], None)
    prompts = [{"id": "p1", "text": "what is acme", "score": 0.1}]

    artifacts = asyncio.run(audit_pipeline._generate_rewrite_artifacts(
        state, {"domain": "acme.com"}, [], prompts, {"informational": 0.1},
    ))

    assert artifacts[0]["status"] == "failed"
    assert "429" in artifacts[0]["error"]
    assert artifacts[0]["bucket"] == "informational"


def test_generate_rewrite_artifacts_uses_already_crawled_competitor_pages(monkeypatch):
    captured = {}

    async def fake_generate_artifact(artifact_type, *, winning_evidence, **kw):
        captured["evidence"] = winning_evidence
        return _artifact_stub(artifact_type)

    monkeypatch.setattr(audit_pipeline.rewrite_engine, "generate_artifact", fake_generate_artifact)
    state = audit_store.create_audit("u5", "acme.com", [], None)
    competitor_sites = [{
        "domain": "rival.com",
        "pages": [{
            "url": "https://rival.com", "headings": ["Why Rival"],
            "content_text": "Rival is great " * 20, "page_type": "homepage", "title": "Rival",
        }],
    }]

    asyncio.run(audit_pipeline._generate_rewrite_artifacts(
        state, {"domain": "acme.com"}, competitor_sites,
        [{"id": "p1", "text": "what is acme", "score": 0.1}], {"informational": 0.1},
    ))

    # evidence came from the already-crawled page, not a second live fetch
    assert captured["evidence"][0]["url"] == "https://rival.com"


# --- 429 quota vs. generic error classification -------------------------

def test_audit_failure_for_429_is_quota():
    exc = GeminiError("Gemini request failed: HTTP 429: quota exceeded", status_code=429)
    failure = audit_pipeline._audit_failure_for(exc, context="generating buyer prompts")
    assert failure.error_type == "quota"
    assert "quota" in str(failure).lower()
    assert "try again later" in str(failure).lower()


def test_audit_failure_for_non_429_is_generic():
    exc = GeminiError("Gemini request failed: HTTP 503: unavailable", status_code=503)
    failure = audit_pipeline._audit_failure_for(exc, context="evaluating prompts mid-audit")
    assert failure.error_type == "generic"
    assert "quota" not in str(failure).lower()


def test_gemini_client_captures_429_status_code(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    get_settings.cache_clear()
    try:
        def handler(request):
            return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            return real_async_client(transport=transport)

        monkeypatch.setattr(gemini_client_mod.httpx, "AsyncClient", fake_async_client)

        client = GeminiClient()
        with pytest.raises(GeminiError) as exc_info:
            asyncio.run(client.generate_text(system_instruction="x", user_prompt="y"))
        assert exc_info.value.status_code == 429
    finally:
        get_settings.cache_clear()


def _patch_run_audit_up_to_prompt_generation(monkeypatch, prompt_generation_error: GeminiError):
    async def fast_sleep(*a, **kw):
        return None

    async def fake_crawl(domain, **kw):
        return {"domain": domain, "label": domain.split(".")[0].title(),
                "pages": [{"url": f"https://{domain}"}], "page_type_counts": {}}

    async def fake_refine(target_site):
        return None

    async def fake_generate_prompts(state, target_site, competitor_sites):
        raise prompt_generation_error

    monkeypatch.setattr(audit_pipeline.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(audit_pipeline, "crawl_site", fake_crawl)
    monkeypatch.setattr(audit_pipeline, "_refine_target_category_with_gemini", fake_refine)
    monkeypatch.setattr(audit_pipeline, "_generate_prompts_with_gemini", fake_generate_prompts)


def test_run_audit_marks_quota_error_type_on_429(monkeypatch):
    _patch_run_audit_up_to_prompt_generation(
        monkeypatch,
        GeminiError("Gemini request failed: HTTP 429: quota exceeded", status_code=429),
    )
    state = audit_store.create_audit("u6", "acme.com", [], None)
    asyncio.run(audit_pipeline.run_audit(state.id))

    failed = audit_store.get(state.id)
    assert failed.status == "failed"
    assert failed.error_type == "quota"
    assert "quota" in failed.error_message.lower()
    assert "try again later" in failed.error_message.lower()


def test_run_audit_marks_generic_error_type_on_non_429(monkeypatch):
    _patch_run_audit_up_to_prompt_generation(
        monkeypatch,
        GeminiError("Gemini request failed: HTTP 503: unavailable", status_code=503),
    )
    state = audit_store.create_audit("u7", "acme.com", [], None)
    asyncio.run(audit_pipeline.run_audit(state.id))

    failed = audit_store.get(state.id)
    assert failed.status == "failed"
    assert failed.error_type == "generic"
    assert "quota" not in failed.error_message.lower()
