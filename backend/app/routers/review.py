"""Human review gate endpoints for the agentic loop (step 7).

The graph pauses at `human_review` and cannot continue until an owner
decides. These two endpoints are how that decision arrives from outside the
process:

    GET  /audits/{id}/review   what is awaiting review (safe to poll)
    POST /audits/{id}/review   approve / revise / reject, resuming the graph

Both are owner-scoped through the same check the audit endpoints use.

Neither endpoint publishes anything. `published=true` is the owner telling us
they have already shipped the change themselves, which is the only thing that
makes the independent re-test meaningful - it is not an instruction for us to
deploy, and there is no code here that could.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.routers.audits import _owned_audit

router = APIRouter(tags=["review"])
log = logging.getLogger(__name__)


class ReviewDecisionBody(BaseModel):
    decision: Literal["approve", "revise", "reject"]
    # Only meaningful with "approve": the re-test measures the live page, so
    # claiming publication before the change is retrievable would measure the
    # old page and call the result a lift.
    published: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class ReviewStateOut(BaseModel):
    audit_id: str
    awaiting_review: bool
    payload: dict | None = None
    next_nodes: list[str] = Field(default_factory=list)


class ReviewResultOut(BaseModel):
    audit_id: str
    accepted_decision: str
    still_awaiting_review: bool
    decision: str | None = None
    decision_reason: str | None = None
    retest: dict | None = None
    cycle_number: int | None = None


def _graph_unavailable(exc: Exception) -> HTTPException:
    """The durable checkpointer is required; say so instead of 500ing."""
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/audits/{audit_id}/review", response_model=ReviewStateOut)
async def get_review(request: Request, audit_id: str) -> ReviewStateOut:
    _owned_audit(request, audit_id)

    from app.graph.checkpointer import CheckpointerUnavailable
    from app.graph.loop_graph import get_pending_review

    try:
        pending = await get_pending_review(audit_id)
    except CheckpointerUnavailable as exc:
        raise _graph_unavailable(exc) from exc

    if pending is None:
        return ReviewStateOut(audit_id=audit_id, awaiting_review=False)
    return ReviewStateOut(
        audit_id=audit_id,
        awaiting_review=True,
        payload=pending.get("payload") if isinstance(pending.get("payload"), dict) else None,
        next_nodes=pending.get("next_nodes") or [],
    )


@router.post("/audits/{audit_id}/review", response_model=ReviewResultOut)
async def submit_review(
    request: Request, audit_id: str, body: ReviewDecisionBody
) -> ReviewResultOut:
    _owned_audit(request, audit_id)

    from app.graph.checkpointer import CheckpointerUnavailable
    from app.graph.loop_graph import get_pending_review, resume_graph_cycle

    try:
        pending = await get_pending_review(audit_id)
    except CheckpointerUnavailable as exc:
        raise _graph_unavailable(exc) from exc

    if pending is None:
        raise HTTPException(
            status_code=409,
            detail="This audit's loop is not waiting for review; nothing to resume.",
        )

    # Reviewer identity comes from the verified token, never the request body,
    # so an approval cannot be attributed to someone else.
    user = getattr(request.state, "user", None) or {}
    decision = {
        "decision": body.decision,
        "published": body.published if body.decision == "approve" else False,
        "reviewer": user.get("email"),
        "notes": body.notes,
    }
    log.info(
        "Review decision for audit %s: %s (published=%s)",
        audit_id, body.decision, decision["published"],
    )

    try:
        state = await resume_graph_cycle(audit_id, decision)
    except CheckpointerUnavailable as exc:
        raise _graph_unavailable(exc) from exc

    return ReviewResultOut(
        audit_id=audit_id,
        accepted_decision=body.decision,
        still_awaiting_review="__interrupt__" in state,
        decision=state.get("decision"),
        decision_reason=state.get("decision_reason"),
        retest=state.get("retest"),
        cycle_number=state.get("cycle_number"),
    )
