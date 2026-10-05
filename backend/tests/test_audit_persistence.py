"""Persistent audit state survives losing local process memory."""
from app import audit_store


class Result:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self):
        self.rows = {}
        self.filters = []

    def upsert(self, row, **kwargs):
        self.pending = row
        return self

    def select(self, *args):
        self.filters = []
        self.pending = None
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, *args):
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        if self.pending is not None:
            self.rows[self.pending["id"]] = dict(self.pending)
            return Result([self.pending])
        return Result([row for row in self.rows.values()
                       if all(row.get(k) == v for k, v in self.filters)])


def test_audit_survives_store_reinitialization(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(audit_store, "_db", lambda: table)
    created = audit_store.create_audit("u1", "Example.com", [], None)
    audit_store.update_progress(created.id, stage="evaluating", progress_percent=64)
    audit_store._audits.clear()
    audit_store._user_audit_ids.clear()
    restored = audit_store.get(created.id)
    assert restored is not None
    assert (restored.primary_domain, restored.stage, restored.progress_percent) == ("example.com", "evaluating", 64)
    assert [a.id for a in audit_store.list_for_user("u1")] == [created.id]
    assert audit_store.list_for_user("u2") == []
    audit_store.complete_audit(created.id, visibility_score=0.25, target_mention_rate=0.6,
                               competitor_scores=[], prompts=[{"text": "best example?"}],
                               recommendations=[], crawl_summary={})
    audit_store._audits.clear()
    assert audit_store.get(created.id).visibility_score == 0.25


def test_missing_table_falls_back_to_memory(monkeypatch, caplog):
    class Missing:
        def __getattr__(self, name):
            def call(*a, **k):
                return self
            return call

        def execute(self):
            raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table 'public.visibility_audits' in the schema cache\"}")

    monkeypatch.setattr(audit_store, "_table_missing", False)
    monkeypatch.setattr(audit_store, "_db", lambda: None if audit_store._table_missing else Missing())
    audit_store._audits.clear()
    audit_store._user_audit_ids.clear()
    created = audit_store.create_audit("u9", "fallback.io", [], None)
    assert audit_store._table_missing is True
    assert audit_store.get(created.id).primary_domain == "fallback.io"
    assert [a.id for a in audit_store.list_for_user("u9")] == [created.id]
    assert "visibility_audits" in caplog.text


def test_other_db_errors_still_raise(monkeypatch):
    class Boom:
        def upsert(self, *a, **k):
            return self

        def execute(self):
            raise RuntimeError("Invalid API key")

    monkeypatch.setattr(audit_store, "_table_missing", False)
    monkeypatch.setattr(audit_store, "_db", lambda: Boom())
    import pytest
    with pytest.raises(RuntimeError):
        audit_store.create_audit("u8", "x.io", [], None)
