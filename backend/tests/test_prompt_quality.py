"""Regression tests for prompt-generation quality: human-sounding phrasing,
branded-prompt tagging/capping, and the per-prompt explanation variety fix.

Background: generated "buyer prompts" were reading like SEO keyword stacks
("what tool do fast-growing software startups use instead of jira for
roadmap planning") rather than things a real person types into an AI chat.
Separately, `names_brand` only checked the target's own name, so prompts
naming a competitor only (e.g. "best alternatives to jira") silently counted
as "unbranded" and could inflate the headline visibility score.

A later pass fixed a second bug in the same area: the "branded" reference
bucket was generating prompts that name the COMPETITOR instead of the target
(e.g. "is calendly worth it" on a cal.com audit). Branded prompts exist to
test whether answers stay accurate when the buyer names OUR brand, so only
target mentions should land in that bucket; a prompt naming only a competitor
is dropped entirely rather than shown anywhere.
"""

from types import SimpleNamespace

from app.audit_pipeline import (
    BRANDED_PROMPT_CAP,
    PROMPT_TARGET_MIN,
    GeminiError,
    _finalize_prompt_set,
    _looks_like_real_buyer_query,
    _short_descriptor,
    _prompt_looks_polluted,
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


def test_accepts_category_shopping_queries_with_longer_content_runs():
    """Measured false positives on a real posthog.com run.

    The keyword-stack guard counted any run of 5+ non-stopword words as a
    stack, but its function-word list was missing ordinary words like
    "without", "from", "anyone" and "using". That systematically rejected the
    exact unbranded category queries this product exists to test, while short
    branded prompts ("posthog vs amplitude reddit") sailed through - so the
    surviving set skewed branded and the unbranded floor kept failing.
    """
    good_examples = [
        "best product analytics tool for a startup",
        "easiest way to track user drop off without massive setup",
        "cheap session replay tool for a small saas team",
        "what do dev teams use to analyze conversion rates",
        "which analytics tool should i use for my mobile app",
        "anyone know a good product analytics tool for early stage saas",
        "how much should i expect to pay for product analytics",
    ]
    for text in good_examples:
        assert _looks_like_real_buyer_query(text), f"should accept: {text!r}"


def test_still_rejects_noun_phrase_stacks_after_widening_the_stopword_list():
    """The widened function-word list must not re-admit keyword stacks. These
    are product-description noun phrases, not anything a person types."""
    bad_examples = [
        "enterprise grade product analytics experimentation platform software",
        "scalable product analytics experimentation feature management platform",
        "can ai tools generate bug fixes from usage data",
    ]
    for text in bad_examples:
        assert not _looks_like_real_buyer_query(text), f"should reject: {text!r}"


def test_vendor_capability_echo_is_a_generation_concern_not_a_filter_one():
    """Documents a real limitation rather than over-fitting the filter.

    PostHog's homepage says it "automatically diagnoses problems, fixes bugs,
    and generates pull requests", and on a live run the model echoed that back
    as supposed buyer queries - the complaint being fixed. But structurally
    "anyone using X that writes Y" is ordinary speech; only its MEANING marks
    it as vendor copy. Making the run-length guard reject it would restore the
    over-aggression that was dropping real category queries, so this register
    is suppressed by the generation instructions instead. Asserting the
    filter's blind spot keeps that division of labour honest: if someone later
    makes this pass the filter, they should know it was never the filter's job.
    """
    assert _looks_like_real_buyer_query(
        "anyone using product analytics tools that write pull requests"
    ), "structural guard cannot and should not judge semantic vendor-copy echo"


def test_single_nav_word_in_a_natural_question_is_not_treated_as_menu_text():
    """"support", "pricing" and "docs" are nav labels but also ordinary buyer
    vocabulary. Matching any ONE of them dropped legitimate pricing and trust
    queries, skewing those buckets toward no coverage."""
    keepers = [
        "are people actually happy with posthog support",
        "how much does posthog cost compared to amplitude pricing",
        "are the docs good enough to self serve",
    ]
    for text in keepers:
        assert not _prompt_looks_polluted(text, []), f"should keep: {text!r}"


def test_scraped_navigation_menu_text_is_still_rejected():
    assert _prompt_looks_polluted("Products Solutions Developers Resources Pricing", [])
    assert _prompt_looks_polluted("docs pricing login contact company", [])


def test_rejects_prompts_that_copy_the_sites_own_marketing_phrasing():
    """A buyer choosing a tool types the category ("product analytics"), not
    the vendor's tagline ("your product's context layer"). Verbatim multi-word
    lifts from the crawled summary/keywords are vendor copy, not buyer speech.
    """
    blocked = [
        "your product s context layer",
        "product analytics and experimentation platform",
        "give agents product context",
    ]
    assert _prompt_looks_polluted("is your product s context layer worth it", blocked)
    assert _prompt_looks_polluted("best product analytics and experimentation platform", blocked)


def test_category_words_themselves_stay_usable_in_buyer_queries():
    """The guard must block copied phrases, not the category noun itself -
    buyers really do type "product analytics"."""
    blocked = ["product analytics and experimentation platform", "product analytics"]
    assert not _prompt_looks_polluted("best product analytics tool for a startup", blocked)
    assert not _prompt_looks_polluted("cheapest product analytics for early stage", blocked)


def test_buyer_descriptor_rejects_scraped_marketing_sentences():
    """The category/buyer strings get interpolated into the generation
    instructions as the shape to imitate, so they must be short descriptors.

    When the Gemini category refinement fails, run_audit logs it and carries on
    with the heuristic distillation - where `audiences[0]` for posthog.com is
    the scraped banner "Join 500,000+ teams already shipping with PostHog".
    Interpolating that would instruct the model to write
    "best <category> for Join 500,000+ teams already shipping with PostHog",
    injecting the exact vendor copy this change exists to remove, and leaking
    the brand into prompts that are supposed to be unbranded.
    """
    assert _short_descriptor("software product teams", "Posthog") == "software product teams"
    assert _short_descriptor("product analytics", "Posthog") == "product analytics"

    assert _short_descriptor("Join 500,000+ teams already shipping with PostHog", "Posthog") == ""
    assert _short_descriptor("The single platform for engineers to analyze, test, observe", "Posthog") == ""
    # must never hand back something naming the brand: these instructions
    # drive the UNBRANDED pass
    assert _short_descriptor("posthog users", "Posthog") == ""
    assert _short_descriptor("", "Posthog") == ""


# --- branded prompt tagging and capping ----------------------------------------------


def test_tags_prompts_by_who_they_name():
    cleaned = [
        {"id": "1", "text": "is linear worth it", "intent": "trust"},
        {"id": "2", "text": "best alternatives to jira for small teams", "intent": "comparative"},
        {"id": "3", "text": "best issue tracker for a small team", "intent": "informational"},
        {"id": "4", "text": "linear vs jira reddit", "intent": "comparative"},
    ]
    _tag_names_brand(cleaned, FULL_SITE, [COMPETITOR_SITE])
    by_text = {p["text"]: p for p in cleaned}

    assert by_text["is linear worth it"]["names_brand"] is True
    assert by_text["is linear worth it"]["names_competitor_only"] is False

    assert by_text["best alternatives to jira for small teams"]["names_brand"] is True
    assert by_text["best alternatives to jira for small teams"]["names_competitor_only"] is True, (
        "naming only the competitor (never the target) must be flagged competitor-only"
    )

    assert by_text["best issue tracker for a small team"]["names_brand"] is False
    assert by_text["best issue tracker for a small team"]["names_competitor_only"] is False

    # Names both - this is a valid "branded" comparison prompt, not competitor-only,
    # because it also names the target.
    assert by_text["linear vs jira reddit"]["names_brand"] is True
    assert by_text["linear vs jira reddit"]["names_competitor_only"] is False


def test_competitor_only_prompts_are_dropped_not_shown_as_branded():
    """Reproduces the exact reported bug: a cal.com audit must not surface
    "is calendly worth it" or "switch from calendly credit" as branded prompts -
    they name only the competitor and test nothing about the target brand."""
    cal_site = {
        "domain": "cal.com",
        "label": "Cal",
        "pages": [{"url": "https://cal.com"}],
        "page_type_counts": {"homepage": 1},
    }
    calendly_site = {
        "domain": "calendly.com",
        "label": "Calendly",
        "pages": [{"url": "https://calendly.com"}],
        "page_type_counts": {"homepage": 1},
    }
    cleaned = [
        {"id": f"u{i}", "text": f"unbranded scheduling question number {i}", "intent": "informational"}
        for i in range(PROMPT_TARGET_MIN)
    ] + [
        {"id": "b1", "text": "is cal worth it", "intent": "trust"},
        {"id": "c1", "text": "is calendly worth it anymore", "intent": "comparative"},
        {"id": "c2", "text": "switch from calendly credit", "intent": "transactional"},
    ]
    result = _finalize_prompt_set(cleaned, cal_site, [calendly_site])

    texts = [p["text"] for p in result]
    assert "is calendly worth it anymore" not in texts
    assert "switch from calendly credit" not in texts
    assert "is cal worth it" in texts
    branded_kept = [p for p in result if p["names_brand"]]
    assert all(p["text"] == "is cal worth it" for p in branded_kept)


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
