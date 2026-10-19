"""The review-gate pause must survive the process dying.

This is the one property that an in-memory checkpointer cannot provide, and
the reason the production path requires Postgres. It is proved here by
actually launching two separate interpreters via scripts/verify_graph_pause.py:
the first pauses the graph and exits, the second - which shares no memory with
it - finds the pause in the checkpointer file and resumes it.

Scope: the saver used here is file-backed SQLite, not Postgres. That proves
the cross-process mechanism and the graph's resume path, NOT that Supabase
Postgres specifically works; that needs DATABASE_URL and is run by the owner
with `--saver postgres` (see PLAN.md). No Gemini or answer-engine call is made
by either process - the script installs fakes.
"""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")
pytest.importorskip(
    "langgraph.checkpoint.sqlite",
    reason="langgraph-checkpoint-sqlite not installed (see requirements-dev.txt)",
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_graph_pause.py"


def _run(phase: str, db: Path, audit_id: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--phase", phase, "--saver", "sqlite",
         "--sqlite-path", str(db), "--audit-id", audit_id, *extra],
        capture_output=True, text=True, timeout=180,
    )


def test_pause_is_found_and_resumed_by_a_separate_process(tmp_path):
    db = tmp_path / "pause.sqlite"
    audit_id = str(uuid.uuid4())

    first = _run("start", db, audit_id)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "PAUSED" in first.stdout
    # The pause outlived the process that created it.
    assert db.exists() and db.stat().st_size > 0

    second = _run("resume", db, audit_id, "--decision", "reject")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "FOUND PAUSE in a fresh process" in second.stdout
    assert "next=['human_review']" in second.stdout
    assert "artifacts awaiting review: 1" in second.stdout
    assert "RESUMED" in second.stdout
    assert "owner rejected the drafts" in second.stdout


def test_approving_as_published_retests_after_the_restart(tmp_path):
    db = tmp_path / "pause.sqlite"
    audit_id = str(uuid.uuid4())

    assert _run("start", db, audit_id).returncode == 0
    out = _run("resume", db, audit_id, "--decision", "approve", "--published")

    assert out.returncode == 0, out.stdout + out.stderr
    assert "RESUMED" in out.stdout
    assert "measured_on_frozen_prompt_set" in out.stdout, (
        "approve+published must run the independent re-test after resume"
    )


def test_resuming_an_unknown_thread_reports_not_paused(tmp_path):
    db = tmp_path / "empty.sqlite"
    out = _run("resume", db, str(uuid.uuid4()))

    assert out.returncode == 1
    assert "NOT PAUSED" in out.stdout


def test_resume_does_not_find_a_pause_written_under_a_different_thread(tmp_path):
    """Thread isolation: one audit's pause must not resume another's."""
    db = tmp_path / "pause.sqlite"
    assert _run("start", db, str(uuid.uuid4())).returncode == 0

    out = _run("resume", db, str(uuid.uuid4()))
    assert out.returncode == 1
    assert "NOT PAUSED" in out.stdout
