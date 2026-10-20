from __future__ import annotations

import asyncio
import logging

from app import audit_pipeline
from app.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="visibility.run_audit")
def run_audit_task(audit_id: str) -> str:
    """Run one audit to completion inside a Celery worker.

    Not auto-retried: a retry would re-spend Gemini quota. run_audit records
    its own failure state on the audit row.
    """
    asyncio.run(audit_pipeline.run_audit(audit_id))
    return audit_id
