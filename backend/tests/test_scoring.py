"""Regression tests for competitor visibility scoring symmetry.

Bug (PLAN.md item 2): a competitor with zero mentions in any answer could still
score 70 because `_build_competitor_scores` gave competitors page-completeness
bonuses (has pages / has a comparison page / has a review page) directly inside
the visibility score, using a different formula and a different prompt
denominator than the target. Fix: competitors are scored with the exact same
per-prompt formula, weights, and unbranded-prompt subset as the target; page
completeness is reported separately as `content_readiness`.
"""

from types import SimpleNamespace

from app.audit_pipeline import (
    WEAK_BUCKET_THRESHOLD,
    _brand_variants,
    _build_competitor_scores,
    _compute_prompt_score,
    _find_brand,
    _format_rationale,
)

FULL_SITE = {
    "domain": "target.com",
    "label": "Target",
    "pages": [{"url": "https://target.com"}],
    "page_type_counts": {"homepage": 1, "comparison": 1, "reviews": 1},
}


def _state(domain: str = "target.com") -> SimpleNamespace:
    return SimpleNamespace(primary_domain=domain)


def _prompt(*, text: str, target_role: str, competitor_mentions: list[str], score: float, names_brand: bool = False) -> dict:
    return {
        "id": text,
        "text": text,
        "intent": "informational",
        "names_brand": names_brand,
        "mentioned": target_role != "absent",
        "score": score,
        "competitor_mentions": competitor_mentions,
        "score_components": {"target_role": target_role},
    }


def test_zero_mentions_scores_zero_for_everyone_despite_full_content():
    """The literal reported bug: a fully-built competitor site must not score 70 on silence."""
    competitor_site = {**FULL_SITE, "domain": "acme.com", "label": "Acme"}
    prompts = [
        _prompt(text="what is the best tool for this job?", target_role="absent", competitor_mentions=[], score=0.0),
        _prompt(text="how do teams handle this problem?", target_role="absent", competitor_mentions=[], score=0.0),
        _prompt(text="what should a small team use here?", target_role="absent", competitor_mentions=[], score=0.0),
    ]

    overall_score, scores, target_mention_rate, _ = _build_competitor_scores(
        _state(), prompts, FULL_SITE, [competitor_site]
    )

    assert overall_score == 0.0
    assert target_mention_rate == 0.0
    acme = next(s for s in scores if s["label"] == "Acme")
    assert acme["score"] == 0.0
    # Content completeness is still visible, just not blended into the visibility score.
    assert acme["content_readiness"] > 0.0


def test_competitor_uses_identical_formula_to_target():
    """Swapping who's 'the target' and who's 'the competitor' must not change the score."""
    texts = [
        "what is the best tool for this job?",
        "how do teams handle this problem?",
        "what should a small team use here?",
    ]

    # Scenario A: target is the only entity, mentioned first (central) on every prompt.
    scenario_a_prompts = []
    for text in texts:
        p = _prompt(text=text, target_role="central", competitor_mentions=[], score=0.0)
        score, _ = _compute_prompt_score(
            target_role="central",
            competitor_role="none",
            fit_score=0.85,
            prompt=p,
            target_site=FULL_SITE,
            competitor_mentions=[],
        )
        p["score"] = score
        p["mentioned"] = True
        scenario_a_prompts.append(p)

    overall_score_a, _, _, _ = _build_competitor_scores(_state(), scenario_a_prompts, FULL_SITE, [])

    # Scenario B: target is silent throughout; a competitor ("Acme") with the SAME
    # site content profile is mentioned first (central) on every prompt instead.
    competitor_site = {**FULL_SITE, "domain": "acme.com", "label": "Acme"}
    scenario_b_prompts = [
        _prompt(text=text, target_role="absent", competitor_mentions=["Acme"], score=0.0) for text in texts
    ]

    _, scores_b, _, _ = _build_competitor_scores(_state(), scenario_b_prompts, FULL_SITE, [competitor_site])
    acme = next(s for s in scores_b if s["label"] == "Acme")

    assert acme["score"] == overall_score_a
    assert overall_score_a > 0.0  # sanity check the scenario isn't trivially zero


def test_competitor_scoring_only_counts_the_same_unbranded_headline_subset():
    """A mention on a branded-only prompt must not count toward the competitor's score."""
    competitor_site = {**FULL_SITE, "domain": "acme.com", "label": "Acme"}
    prompts = [
        _prompt(text="what is the best tool for this job?", target_role="central", competitor_mentions=[], score=0.9),
        _prompt(text="how do teams handle this problem?", target_role="central", competitor_mentions=[], score=0.9),
        # Branded prompt: Acme is mentioned here, but this prompt is excluded from
        # the headline (unbranded) subset that both target and competitors are scored on.
        _prompt(
            text="is target better than acme?",
            target_role="absent",
            competitor_mentions=["Acme"],
            score=0.0,
            names_brand=True,
        ),
    ]

    _, scores, _, _ = _build_competitor_scores(_state(), prompts, FULL_SITE, [competitor_site])

    acme = next(s for s in scores if s["label"] == "Acme")
    assert acme["score"] == 0.0


def test_weak_label_only_applies_below_threshold():
    """A strong bucket score (e.g. 0.96) must never be described as 'weak'."""
    strong_rationale = _format_rationale(
        bucket_name="informational",
        bucket_score=0.96,
        page_coverage_note="no FAQ/help page was found",
    )
    assert "weak" not in strong_rationale.lower(), strong_rationale
    assert "0.96" in strong_rationale

    weak_rationale = _format_rationale(
        bucket_name="informational",
        bucket_score=0.3,
        page_coverage_note="no FAQ/help page was found",
    )
    assert "weak" in weak_rationale.lower(), weak_rationale

    # The two thresholds agree: anything at or above WEAK_BUCKET_THRESHOLD is not "weak".
    boundary_rationale = _format_rationale(
        bucket_name="pricing",
        bucket_score=WEAK_BUCKET_THRESHOLD,
        page_coverage_note="pricing content is missing",
    )
    assert "weak" not in boundary_rationale.lower(), boundary_rationale


def test_competitor_mention_is_detected_from_the_same_answer_text():
    """Competitor mentions must be measured from the real answer text, not assumed."""
    competitor_site = {
        "domain": "acme.com",
        "label": "Acme",
        "pages": [{"url": "https://acme.com"}],
        "page_type_counts": {"homepage": 1},
    }
    variants = _brand_variants(
        competitor_site["label"], competitor_site["domain"], competitor_site
    )

    answer_mentioning_competitor = "for small teams, acme is a popular choice with solid reviews."
    answer_without_competitor = "there are a few solid options depending on your team size."

    assert any(_find_brand(answer_mentioning_competitor, v) >= 0 for v in variants), (
        "a real mention of the competitor's own label must be detected in the answer text"
    )
    assert not any(_find_brand(answer_without_competitor, v) >= 0 for v in variants), (
        "no competitor mention should be detected when the answer never names it"
    )
