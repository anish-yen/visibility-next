from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditState:
    id: str
    user_id: str
    primary_domain: str
    industry: str | None
    competitor_domains: list[str]
    status: str
    stage: str
    progress_percent: int
    visibility_score: float | None
    competitor_scores: list[dict[str, Any]]
    prompts: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    created_at: str
    error_message: str | None = None
    # Crawl output (primary + competitors); not exposed on API to keep payloads small
    crawled_pages: list[dict[str, Any]] = field(default_factory=list)


_audits: dict[str, AuditState] = {}
_user_audit_ids: dict[str, list[str]] = {}


def normalize_domain(raw: str) -> str:
    s = raw.strip().lower()
    s = s.removeprefix("https://").removeprefix("http://")
    s = s.split("/")[0].split("?")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def create_audit(
    user_id: str,
    primary_domain: str,
    competitor_domains: list[str],
    industry: str | None,
) -> AuditState:
    aid = str(uuid.uuid4())
    primary = normalize_domain(primary_domain)
    comps = [normalize_domain(c) for c in competitor_domains if c.strip()][:3]

    state = AuditState(
        id=aid,
        user_id=user_id,
        primary_domain=primary,
        industry=industry.strip() if industry else None,
        competitor_domains=comps,
        status="running",
        stage="crawling",
        progress_percent=5,
        visibility_score=None,
        competitor_scores=[],
        prompts=[],
        recommendations=[],
        created_at=_iso(),
    )
    _audits[aid] = state
    _user_audit_ids.setdefault(user_id, []).insert(0, aid)
    return state


def get(audit_id: str) -> AuditState | None:
    return _audits.get(audit_id)


def list_for_user(user_id: str) -> list[AuditState]:
    ids = _user_audit_ids.get(user_id, [])
    return [_audits[i] for i in ids if i in _audits]


def update_progress(
    audit_id: str,
    *,
    stage: str,
    progress_percent: int,
) -> None:
    a = _audits.get(audit_id)
    if not a:
        return
    a.stage = stage
    a.progress_percent = progress_percent


def fail_audit(audit_id: str, message: str) -> None:
    a = _audits.get(audit_id)
    if not a:
        return
    a.status = "failed"
    a.stage = "failed"
    a.error_message = message
    a.progress_percent = 100


def complete_audit(
    audit_id: str,
    *,
    visibility_score: float,
    competitor_scores: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> None:
    a = _audits.get(audit_id)
    if not a:
        return
    a.status = "completed"
    a.stage = "completed"
    a.progress_percent = 100
    a.visibility_score = visibility_score
    a.competitor_scores = competitor_scores
    a.prompts = prompts
    a.recommendations = recommendations


def attach_brief(audit_id: str, recommendation_id: str, brief: dict[str, Any]) -> bool:
    a = _audits.get(audit_id)
    if not a:
        return False
    for rec in a.recommendations:
        if rec.get("id") == recommendation_id:
            rec["brief"] = brief
            return True
    return False
