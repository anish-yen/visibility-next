"""Celery app for running audits as background jobs.

Broker/result backend: Redis (REDIS_URL). Audit state and results are written
by audit_store to the database (Supabase Postgres) from inside the worker, so
the API process and the worker process share state through the database.

Run a worker:  celery -A app.celery_app.celery_app worker --loglevel=info
"""
from __future__ import annotations

from celery import Celery

from app.config import get_settings


def make_celery() -> Celery:
    url = get_settings().redis_url or "memory://"
    app = Celery("visibility", broker=url, backend=get_settings().redis_url or None, include=["app.tasks"])
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = make_celery()
