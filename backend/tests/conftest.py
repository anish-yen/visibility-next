import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import audit_store


@pytest.fixture(autouse=True)
def _never_touch_real_supabase(monkeypatch):
    """Force every test onto audit_store's in-memory path.

    `app.config` calls load_dotenv(), so a developer's real `backend/.env`
    credentials are live during a test run. That made `audit_store._db()`
    return a real Supabase request builder, so any test calling
    `create_audit` fired an actual upsert at the production project and only
    "passed" because the missing-table error happens to fall back to memory.
    Tests must not write to production, and must not need the network at all.

    Tests that want to exercise the Supabase branch (test_audit_persistence)
    monkeypatch `_db` themselves inside the test body, which runs after this
    fixture and therefore wins.
    """
    monkeypatch.setattr(audit_store, "_db", lambda: None)
    monkeypatch.setattr(audit_store, "_table_missing", False)
    audit_store._audits.clear()
    audit_store._user_audit_ids.clear()
