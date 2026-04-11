from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from app import audit_pipeline, audit_store
from app.schemas_audit import (
    AuditCreateBody,
    AuditDetailOut,
    AuditSummaryOut,
    BriefResponse,
    CompetitorScoreOut,
    ContentBriefOut,
    PromptRowOut,
    RecommendationOut,
)

router = APIRouter(tags=["audits"])


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
                brief=ContentBriefOut(**r["brief"]) if r.get("brief") else None,
            )
            for r in a.recommendations
        ],
        crawl_summary=a.crawl_summary,
        error_message=a.error_message,
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

    state = audit_store.create_audit(
        uid,
        body.primary_domain,
        body.competitor_domains,
        body.industry,
    )
    asyncio.create_task(audit_pipeline.run_audit(state.id))
    return _to_summary(state)


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
