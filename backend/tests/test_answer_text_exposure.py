"""The stored Gemini answer text must reach the owner-only audit detail response.

`_evaluate_prompt` already saves the full answer it graded under each prompt's
"answer" key, but `PromptRowOut` had no such field, so Pydantic silently
dropped it and the report could never show the evidence behind a score. These
tests pin that the answer survives serialization, is only served on the
owner-scoped detail endpoint, and never appears in the audit list payload.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import audit_store
from app.routers import audits as audits_router
from app.schemas_audit import PromptRowOut

ANSWER = (
    "For product analytics at a startup, PostHog and Amplitude are both common "
    "picks. PostHog bundles session replay and feature flags."
)


def _completed_audit(user_id="u1"):
    state = audit_store.create_audit(user_id, "posthog.com", ["amplitude.com"], None)
    audit_store.complete_audit(
        state.id,
        visibility_score=55.0,
        target_mention_rate=0.5,
        competitor_scores=[],
        prompts=[{
            "id": "p1",
            "text": "best product analytics tool for a startup",
            "intent": "informational",
            "mentioned": True,
            "names_brand": False,
            "score": 0.8,
            "explanation": "target mentioned first among tracked brands",
            "competitor_mentions": ["Amplitude"],
            "score_components": {"target_role": "central"},
            "answer": ANSWER,
            "evaluation_source": "gemini_live",
        }],
        recommendations=[],
        crawl_summary={},
    )
    return state


def _app(user_id="u1"):
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user_id = request.headers.get("x-test-user", user_id)
        request.state.user = {"email": "owner@example.com"}
        return await call_next(request)

    app.include_router(audits_router.router)
    return app


def test_prompt_row_schema_preserves_answer_text():
    row = PromptRowOut(
        id="p1", text="best product analytics tool for a startup",
        mentioned=True, score=0.8, answer=ANSWER,
    )
    assert row.answer == ANSWER


def test_prompt_row_answer_defaults_to_none_when_absent():
    """The heuristic fallback path stores no answer; that must not 500."""
    row = PromptRowOut(id="p1", text="anything", mentioned=False, score=0.0)
    assert row.answer is None


def test_owner_detail_response_includes_the_graded_answer_text():
    state = _completed_audit()
    client = TestClient(_app())

    body = client.get(f"/audits/{state.id}").json()

    assert body["prompts"][0]["answer"] == ANSWER


def test_answer_text_is_not_exposed_to_a_non_owner():
    state = _completed_audit(user_id="owner")
    client = TestClient(_app())

    r = client.get(f"/audits/{state.id}", headers={"x-test-user": "someone-else"})

    assert r.status_code == 404
    assert ANSWER not in r.text


def test_audit_list_payload_does_not_carry_answer_text():
    """The list endpoint is a summary; answers are detail-only so one page load
    doesn't ship every answer for every past audit."""
    _completed_audit()
    client = TestClient(_app())

    r = client.get("/audits")

    assert r.status_code == 200
    assert ANSWER not in r.text
