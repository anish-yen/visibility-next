from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import audit_pipeline, audit_store, cycle_store, loop_runner
from app.config import get_settings
from app.schemas_audit import (
    AuditCreateBody,
    AuditDetailOut,
    AuditSummaryOut,
    BriefResponse,
    CheckoutResponseOut,
    CompetitorScoreOut,
    CycleSummaryOut,
    ContentBriefOut,
    PromptRowOut,
    RecommendationOut,
    RewriteArtifactOut,
)
from app.services import stripe_client
from app.services.stripe_client import StripeNotConfigured

router = APIRouter(tags=["audits"])
log = logging.getLogger(__name__)


def _is_qa_whitelisted(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    email = str(user.get("email") or "").strip().lower()
    return bool(email) and email in get_settings().qa_bypass_emails


def _start_audit(user_id: str, primary_domain: str, competitor_domains: list[str], industry: str | None):
    state = audit_store.create_audit(user_id, primary_domain, competitor_domains, industry)
    asyncio.create_task(audit_pipeline.run_audit(state.id))
    return state


def _to_summary(a: audit_store.AuditState) -> AuditSummaryOut:
    return AuditSummaryOut(
        id=a.id,
        primary_domain=a.primary_domain,
        status=a.status,
        stage=a.stage,
        progress_percent=a.progress_percent,
        visibility_score=a.visibility_score,
        target_mention_rate=a.target_mention_rate,
        created_at=a.created_at,
    )


def _to_detail(a: audit_store.AuditState) -> AuditDetailOut:
    return AuditDetailOut(
        **_to_summary(a).model_dump(),
        industry=a.industry,
        competitor_domains=a.competitor_domains,
        competitor_scores=[CompetitorScoreOut(**x) for x in a.competitor_scores],
        prompts=[PromptRowOut(**x) for x in a.prompts],
        recommendations=[
            RecommendationOut(
                id=r["id"],
                title=r["title"],
                rationale=r["rationale"],
                priority_score=r["priority_score"],
                recommendation_evidence=r.get("recommendation_evidence", {}),
                brief=ContentBriefOut(**r["brief"]) if r.get("brief") else None,
            )
            for r in a.recommendations
        ],
        rewrite_artifacts=[RewriteArtifactOut(**x) for x in a.rewrite_artifacts],
        crawl_summary=a.crawl_summary,
        weak_prompt_buckets=a.crawl_summary.get("weak_prompt_buckets", {}),
        score_components=a.crawl_summary.get("score_components", {}),
        error_message=a.error_message,
        error_type=a.error_type,
    )


def _require_owner(request: Request, audit: audit_store.AuditState) -> None:
    uid = getattr(request.state, "user_id", None)
    if not uid or audit.user_id != uid:
        raise HTTPException(status_code=404, detail="Audit not found")


@router.get("/audits", response_model=list[AuditSummaryOut])
def list_audits(request: Request) -> list[AuditSummaryOut]:
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return [_to_summary(a) for a in audit_store.list_for_user(uid)]


@router.post("/audits", response_model=AuditSummaryOut, status_code=201)
async def create_audit(request: Request, body: AuditCreateBody) -> AuditSummaryOut:
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not _is_qa_whitelisted(request):
        raise HTTPException(
            status_code=402,
            detail="Payment required. Start an audit through /checkout.",
        )

    state = _start_audit(uid, body.primary_domain, body.competitor_domains, body.industry)
    return _to_summary(state)


@router.post("/checkout", response_model=CheckoutResponseOut)
async def start_checkout(request: Request, body: AuditCreateBody) -> CheckoutResponseOut:
    """Single entry point the frontend uses to start an audit. QA-whitelisted
    emails skip payment and get an audit immediately, same as the old direct
    POST /audits flow; everyone else gets a Stripe Checkout URL to redirect
    to - the audit itself is only created once the webhook below confirms
    payment."""
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if _is_qa_whitelisted(request):
        state = _start_audit(uid, body.primary_domain, body.competitor_domains, body.industry)
        return CheckoutResponseOut(bypassed=True, audit=_to_summary(state))

    try:
        checkout_url = stripe_client.create_audit_checkout_session(
            user_id=uid,
            primary_domain=body.primary_domain,
            competitor_domains=body.competitor_domains,
            industry=body.industry,
        )
    except StripeNotConfigured as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return CheckoutResponseOut(bypassed=False, checkout_url=checkout_url)


@router.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(request: Request) -> dict:
    """Stripe calls this directly (no Supabase auth - see middleware.py's
    public-path exemption); authenticity comes from the signature check
    inside parse_webhook_event, not from a bearer token."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe_client.parse_webhook_event(payload, signature)
    except StripeNotConfigured as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Stripe signature") from exc

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        if session.get("payment_status") == "unpaid":
            # Delayed-notification payment method: completed fired but the
            # session hasn't actually been paid yet. async_payment_succeeded
            # (or async_payment_failed) will follow later - fulfill then.
            log.info("Stripe session %s completed but still unpaid; awaiting async result", session.get("id"))
        else:
            inputs = stripe_client.audit_inputs_from_session(session)
            if inputs is None:
                log.warning("Stripe session missing expected metadata: %s", session.get("id"))
            else:
                _start_audit(
                    inputs["user_id"],
                    inputs["primary_domain"],
                    inputs["competitor_domains"],
                    inputs["industry"],
                )
    elif event["type"] == "checkout.session.async_payment_failed":
        session = event["data"]["object"]
        log.info("Stripe checkout session payment failed: %s", session.get("id"))

    return {"received": True}


@router.get("/audits/{audit_id}", response_model=AuditDetailOut)
def get_audit(request: Request, audit_id: str) -> AuditDetailOut:
    a = audit_store.get(audit_id)
    if not a:
        raise HTTPException(status_code=404, detail="Audit not found")
    _require_owner(request, a)
    return _to_detail(a)


@router.post(
    "/audits/{audit_id}/recommendations/{recommendation_id}/brief",
    response_model=BriefResponse,
)
async def generate_brief(
    request: Request,
    audit_id: str,
    recommendation_id: str,
) -> BriefResponse:
    a = audit_store.get(audit_id)
    if not a:
        raise HTTPException(status_code=404, detail="Audit not found")
    _require_owner(request, a)
    if a.status != "completed":
        raise HTTPException(status_code=400, detail="Audit is not complete")

    rec = next((r for r in a.recommendations if r.get("id") == recommendation_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    if rec.get("brief"):
        return BriefResponse(
            recommendation_id=recommendation_id,
            brief=ContentBriefOut(**rec["brief"]),
        )

    brief = await audit_pipeline.generate_content_brief(a, rec)
    audit_store.attach_brief(audit_id, recommendation_id, brief)
    return BriefResponse(recommendation_id=recommendation_id, brief=ContentBriefOut(**brief))


def _owned_audit(request: Request, audit_id: str) -> audit_store.AuditState:
    a = audit_store.get(audit_id)
    if not a:
        raise HTTPException(status_code=404, detail="Audit not found")
    _require_owner(request, a)
    return a


def _cycle_summary(s: dict) -> CycleSummaryOut:
    return CycleSummaryOut(
        snapshot_id=s.get("snapshot_id"),
        cycle_number=s.get("cycle_number", 0),
        mention_rate=s.get("mention_rate"),
        lift=s.get("lift"),
        decision=s.get("decision"),
        decision_reason=s.get("decision_reason"),
        created_at=s.get("created_at"),
    )


@router.post("/audits/{audit_id}/cycle", status_code=202)
async def run_cycle_endpoint(
    request: Request, audit_id: str, background_tasks: BackgroundTasks
) -> dict:
    """Kick off one agentic-loop cycle in the background."""
    _owned_audit(request, audit_id)
    background_tasks.add_task(
        loop_runner.run_cycle, audit_id, cycle_store.get_cycle_store()
    )
    return {"status": "running", "audit_id": audit_id}


@router.get("/audits/{audit_id}/cycles", response_model=list[CycleSummaryOut])
async def list_cycles(request: Request, audit_id: str) -> list[CycleSummaryOut]:
    """Cycle history for the frontend, newest first."""
    _owned_audit(request, audit_id)
    snapshots = await cycle_store.get_cycle_store().list(audit_id)
    return [_cycle_summary(s) for s in snapshots]


@router.get("/audits/{audit_id}/cycles/{cycle_number}")
async def get_cycle(request: Request, audit_id: str, cycle_number: int) -> dict:
    """One full cycle snapshot (prompt results, citation map, artifacts)."""
    _owned_audit(request, audit_id)
    snapshot = await cycle_store.get_cycle_store().get(audit_id, cycle_number)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Cycle not found")
    return snapshot
