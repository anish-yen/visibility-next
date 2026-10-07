"""Regression tests for prompt-generation quality: human-sounding phrasing,
branded-prompt tagging/capping, and the per-prompt explanation variety fix.

Background: generated "buyer prompts" were reading like SEO keyword stacks
("what tool do fast-growing software startups use instead of jira for
roadmap planning") rather than things a real person types into an AI chat.
Separately, `names_brand` only checked the target's own name, so prompts
naming a competitor only (e.g. "best alternatives to jira") silently counted
as "unbranded" and could inflate the headline visibility score.
"""

from types import SimpleNamespace

from app.audit_pipeline import (
    BRANDED_PROMPT_CAP,
    PROMPT_TARGET_MIN,
    GeminiError,
    _finalize_prompt_set,
    _looks_like_real_buyer_query,
    _mention_explanation,
    _tag_names_brand,
)

FULL_SITE = {
    "domain": "linear.app",
    "label": "Linear",
    "pages": [{"url": "https://linear.app"}],
    "page_type_counts": {"homepage": 1},
}
COMPETITOR_SITE = {
    "domain": "jira.software",
    "label": "Jira",
    "pages": [{"url": "https://jira.software"}],
    "page_type_counts": {"homepage": 1},
}


def _state() -> SimpleNamespace:
    return SimpleNamespace(primary_domain="linear.app")


# --- human-sounding prompt heuristic -------------------------------------------------


def test_accepts_casual_buyer_phrasing():
    good_examples = [
        "jira is too slow what do startups use",
        "best issue tracker for small team",
        "is linear worth it",
        "linear vs jira reddit",
        "service that audits whether chatgpt mentions your product",
        "alternatives to profound for ai visibility",
        "how do i get my startup cited by ai chatbots",
        "anyone know a good tool for tracking ai mentions",
    ]
    for text in good_examples:
        assert _looks_like_real_buyer_query(text), f"should accept: {text!r}"


def test_rejects_seo_keyword_stacked_phrasing():
    bad_examples = [
        "what tool do fast-growing software startups use instead of jira for roadmap planning",
        "enterprise grade workflow automation software for distributed engineering teams",
        "robust scalable cloud native data pipeline orchestration platform",
    ]
    for text in bad_examples:
        assert not _looks_like_real_buyer_query(text), f"should reject: {text!r}"


# --- branded prompt tagging and capping ----------------------------------------------


def test_tags_prompts_naming_target_or_competitor_as_branded():
    cleaned = [
        {"id": "1", "text": "is linear worth it", "intent": "trust"},
        {"id": "2", "text": "best alternatives to jira for small teams", "intent": "comparative"},
        {"id": "3", "text": "best issue tracker for a small team", "intent": "informational"},
        {"id": "4", "text": "linear pricing plans explained", "intent": "pricing"},
    ]
    _tag_names_brand(cleaned, FULL_SITE, [COMPETITOR_SITE])
    by_text = {p["text"]: p["names_brand"] for p in cleaned}
    assert by_text["is linear worth it"] is True
    assert by_text["best alternatives to jira for small teams"] is True, (
        "a prompt naming only the competitor must still be tagged branded"
    )
    assert by_text["best issue tracker for a small team"] is False
    assert by_text["linear pricing plans explained"] is True


def test_caps_branded_prompts_in_final_set():
    unbranded = [
        {"id": f"u{i}", "text": f"unbranded buyer question number {i}", "intent": "informational"}
        for i in range(PROMPT_TARGET_MIN)
    ]
    branded = [
        {"id": f"b{i}", "text": f"linear branded question number {i}", "intent": "trust"}
        for i in range(5)
    ]
    result = _finalize_prompt_set(unbranded + branded, FULL_SITE, [COMPETITOR_SITE])
    branded_kept = [p for p in result if p["names_brand"]]
    assert len(branded_kept) <= BRANDED_PROMPT_CAP
    unbranded_kept = [p for p in result if not p["names_brand"]]
    assert len(unbranded_kept) == PROMPT_TARGET_MIN


def test_raises_when_unbranded_count_below_minimum():
    mostly_branded = [
        {"id": f"b{i}", "text": f"linear branded question number {i}", "intent": "trust"}
        for i in range(PROMPT_TARGET_MIN)
    ]
    try:
        _finalize_prompt_set(mostly_branded, FULL_SITE, [COMPETITOR_SITE], min_unbranded=PROMPT_TARGET_MIN)
        assert False, "expected GeminiError for insufficient unbranded prompts"
    except GeminiError:
        pass


def test_fallback_mode_does_not_raise_without_minimum():
    mostly_branded = [{"id": "b1", "text": "linear branded question", "intent": "trust"}]
    result = _finalize_prompt_set(mostly_branded, FULL_SITE, [COMPETITOR_SITE])
    assert len(result) == 1


# --- per-prompt explanation variety ---------------------------------------------------


def test_explanation_varies_across_prompts_but_is_stable_per_prompt():
    explanations = {
        _mention_explanation(
            prompt_id=f"prompt-{i}",
            target_role="central",
            competitor_mentions=[],
            source_label="Real Gemini answer",
        )
        for i in range(12)
    }
    assert len(explanations) > 1, "explanation text should not be identical for every prompt"

    first = _mention_explanation(
        prompt_id="stable-id",
        target_role="supporting",
        competitor_mentions=["Acme"],
        source_label="Real Gemini answer",
    )
    second = _mention_explanation(
        prompt_id="stable-id",
        target_role="supporting",
        competitor_mentions=["Acme"],
        source_label="Real Gemini answer",
    )
    assert first == second, "same prompt id must produce the same explanation on reload"
