from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

TABLE = "visibility_audits"
log = logging.getLogger(__name__)


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
    target_mention_rate: float | None
    competitor_scores: list[dict[str, Any]]
    prompts: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    crawl_summary: dict[str, Any]
    created_at: str
    error_message: str | None = None
    error_type: str | None = None
    rewrite_artifacts: list[dict[str, Any]] = field(default_factory=list)


# In-memory storage is only for local development/tests without Supabase credentials.
_audits: dict[str, AuditState] = {}
_user_audit_ids: dict[str, list[str]] = {}


# Set when Supabase reports the audits table is missing (migration not applied).
_table_missing = False


def _is_missing_table(exc: Exception) -> bool:
    text = f"{getattr(exc, 'code', '')} {exc}".lower()
    return TABLE in text and (
        "pgrst205" in text or "does not exist" in text or "could not find the table" in text
    )


def _fallback(exc: Exception) -> bool:
    """Switch to in-memory storage if the table is missing; return True if handled."""
    global _table_missing
    if not _is_missing_table(exc):
        return False
    if not _table_missing:
        log.warning(
            "Table %s not found in Supabase; using in-memory audit storage "
            "(audits will not survive restarts). Apply backend/supabase/migrations/visibility_audits.sql.",
            TABLE,
        )
    _table_missing = True
    return True


def _db():
    if _table_missing:
        return None
    settings = get_settings()
    if not (settings.supabase_url and settings.supabase_service_key):
        return None
    from app.supabase_client import get_supabase_admin

    return get_supabase_admin().table(TABLE)


def _save(audit: AuditState) -> None:
    db = _db()
    if db is not None:
        try:
            db.upsert(asdict(audit), on_conflict="id").execute()
            return
        except Exception as exc:
            if not _fallback(exc):
                raise
    _audits[audit.id] = audit
    ids = _user_audit_ids.setdefault(audit.user_id, [])
    if audit.id not in ids:
        ids.insert(0, audit.id)


def _from_row(row: dict[str, Any]) -> AuditState:
    values = {name: row.get(name) for name in AuditState.__dataclass_fields__}
    for name in ("competitor_domains", "competitor_scores", "prompts", "recommendations", "rewrite_artifacts"):
        values[name] = values[name] or []
    values["crawl_summary"] = values["crawl_summary"] or {}
    return AuditState(**values)


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
    state = AuditState(
        id=str(uuid.uuid4()),
        user_id=user_id,
        primary_domain=normalize_domain(primary_domain),
        industry=industry.strip() if industry else None,
        competitor_domains=[normalize_domain(c) for c in competitor_domains if c.strip()][:3],
        status="running",
        stage="crawling",
        progress_percent=5,
        visibility_score=None,
        target_mention_rate=None,
        competitor_scores=[],
        prompts=[],
        recommendations=[],
        crawl_summary={},
        created_at=_iso(),
    )
    _save(state)
    return state


def get(audit_id: str) -> AuditState | None:
    db = _db()
    if db is None:
        return _audits.get(audit_id)
    try:
        rows = db.select("*").eq("id", audit_id).limit(1).execute().data or []
    except Exception as exc:
        if not _fallback(exc):
            raise
        return _audits.get(audit_id)
    return _from_row(rows[0]) if rows else None


def list_for_user(user_id: str) -> list[AuditState]:
    db = _db()
    if db is None:
        return [_audits[i] for i in _user_audit_ids.get(user_id, []) if i in _audits]
    try:
        rows = db.select("*").eq("user_id", user_id).order("created_at", desc=True).execute().data or []
    except Exception as exc:
        if not _fallback(exc):
            raise
        return [_audits[i] for i in _user_audit_ids.get(user_id, []) if i in _audits]
    return [_from_row(row) for row in rows]


def update_progress(audit_id: str, *, stage: str, progress_percent: int) -> None:
    a = get(audit_id)
    if a is None:
        return
    a.stage = stage
    a.progress_percent = progress_percent
    _save(a)


def fail_audit(audit_id: str, message: str, *, error_type: str | None = None) -> None:
    a = get(audit_id)
    if a is None:
        return
    a.status = "failed"
    a.stage = "failed"
    a.error_message = message
    a.error_type = error_type
    a.progress_percent = 100
    _save(a)


def complete_audit(
    audit_id: str,
    *,
    visibility_score: float,
    target_mention_rate: float,
    competitor_scores: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    crawl_summary: dict[str, Any],
    rewrite_artifacts: list[dict[str, Any]] | None = None,
) -> None:
    a = get(audit_id)
    if a is None:
        return
    a.status = "completed"
    a.stage = "completed"
    a.progress_percent = 100
    a.visibility_score = visibility_score
    a.target_mention_rate = target_mention_rate
    a.competitor_scores = competitor_scores
    a.prompts = prompts
    a.recommendations = recommendations
    a.crawl_summary = crawl_summary
    a.rewrite_artifacts = rewrite_artifacts or []
    _save(a)


def attach_brief(audit_id: str, recommendation_id: str, brief: dict[str, Any]) -> bool:
    a = get(audit_id)
    if a is None:
        return False
    for rec in a.recommendations:
        if rec.get("id") == recommendation_id:
            rec["brief"] = brief
            _save(a)
            return True
    return False
