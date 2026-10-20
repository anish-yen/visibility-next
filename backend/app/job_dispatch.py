from __future__ import annotations

import asyncio
import logging

from app import audit_pipeline
from app.config import get_settings

log = logging.getLogger(__name__)


def enqueue_audit(audit_id: str) -> str:
    """Queue an audit. Returns "celery" or "inline".

    With REDIS_URL set the audit goes to a Celery worker via Redis. Without
    it (local dev, existing tests) it runs in-process as before. If Redis is
    configured but the enqueue fails, fall back to inline so a paid audit is
    never dropped.
    """
    if get_settings().redis_url:
        try:
            from app.tasks import run_audit_task

            run_audit_task.delay(audit_id)
            return "celery"
        except Exception:
            log.exception("Celery enqueue failed for %s; running inline", audit_id)
    asyncio.create_task(audit_pipeline.run_audit(audit_id))
    return "inline"
