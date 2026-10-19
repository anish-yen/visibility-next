#!/usr/bin/env python
"""Prove the review-gate pause survives a process restart.

Two separate process invocations, deliberately: the point is that the pause
lives in the checkpointer's database and not in process memory, which can
only be shown by letting the first process exit.

    # phase 1 - runs the graph until it pauses at the review gate, then exits
    python scripts/verify_graph_pause.py --phase start --audit-id <uuid>

    # phase 2 - a BRAND NEW process picks the pause back up and resumes it
    python scripts/verify_graph_pause.py --phase resume --audit-id <uuid>

Savers:
    --saver postgres   (default) uses DATABASE_URL. This is the real target.
    --saver sqlite --sqlite-path /tmp/x.sqlite   file-backed, no server needed;
                       used by tests/test_pause_survives_restart.py to prove
                       the cross-process mechanism without a database.

SAFETY: every crawler / answer-engine / rewrite call is replaced with a fake
below, so this script makes NO Gemini call, NO answer-engine call and no
outbound network request, and costs nothing to run. It also does not read or
write the visibility_audits table - the audit record is faked in memory.

What it DOES write, with --saver postgres: LangGraph's own checkpoint tables
(checkpoints, checkpoint_writes, checkpoint_blobs, ...) are created by
saver.setup() in whatever database DATABASE_URL points at.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

# Run from anywhere without setting PYTHONPATH: backend/ is this file's parent.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- fakes: keep this script free of real calls -------------------------

PROMPTS = [
    {"id": f"p{i}", "text": f"buyer question {i}", "intent": "informational"}
    for i in range(4)
]
TARGET_SITE = {
    "domain": "example.com",
    "label": "Example",
    "page_type_counts": {"homepage": 1},
    "pages": [{
        "url": "https://example.com",
        "title": "Example",
        "content_text": "Example helps teams. Trusted by 40 teams.",
        "headings": ["Why Example"],
    }],
}


class _FakeAnswer:
    def __init__(self) -> None:
        self.engine = "fake"
        self.prompt_text = "q"
        self.answer_text = "Example is a good pick"
        self.cited_urls = ["https://example.com/x"]
        self.error = None
        self.mentioned_domains = ["example.com"]
        self.mentioned_labels = []


def _install_fakes(audit_id: str) -> None:
    from app import audit_store
    from app.graph import nodes

    async def fake_crawl(domain, **kw):
        return dict(TARGET_SITE, domain=domain)

    async def fake_run_engines(prompt_text, *, engines=()):
        return [_FakeAnswer()]

    def fake_evaluate(prompt, answers, t_label, t_domain, c_labels, c_domains):
        return {
            "id": prompt["id"], "text": prompt["text"], "intent": prompt.get("intent"),
            "mentioned": prompt["id"] == "p0", "score": 1.0 if prompt["id"] == "p0" else 0.0,
            "explanation": "fake", "competitor_mentions": [],
            "score_components": {}, "answers": [],
        }

    async def fake_tech_checks(domain, html, distilled, counts):
        return [{"check": "llms_txt", "passed": False, "detail": "missing"}]

    async def no_html(url):
        return ""

    async def fake_generate_prompts(state, target, competitors):
        return [dict(p) for p in PROMPTS]

    async def fake_gather(sources):
        return []

    async def fake_generate_artifact(artifact_type, **kw):
        return {
            "artifact_type": artifact_type, "status": "generated", "format": "markdown",
            "content": "Example helps teams. Trusted by 40 teams.",
            "changes_vs_pattern": [], "placeholders": [], "grounded_in": [],
        }

    fake_audit = audit_store.AuditState(
        id=audit_id, user_id="00000000-0000-0000-0000-000000000000",
        primary_domain="example.com", industry=None, competitor_domains=[],
        status="running", stage="x", progress_percent=0,
        visibility_score=None, target_mention_rate=None, competitor_scores=[],
        prompts=[], recommendations=[], crawl_summary={},
        created_at="2026-01-01T00:00:00+00:00",
    )
    # Never touch the real audits table.
    audit_store.get = lambda _id: fake_audit  # type: ignore[assignment]

    nodes.crawl_site = fake_crawl
    nodes.run_engines = fake_run_engines
    nodes._evaluate_with_real_engines = fake_evaluate
    nodes.run_tech_checks = fake_tech_checks
    nodes._fetch_html = no_html
    nodes.distill_site_context = lambda s: {}
    nodes._generate_prompts_with_gemini = fake_generate_prompts
    nodes._bucket_average_scores = lambda r: {"informational": 0.1}
    nodes.build_citation_map = lambda a, owned_domains=None: {
        "total_citations": len(a), "owned_citation_share": 0.0, "winning_sources": [],
    }
    nodes.rewrite_engine.gather_winning_evidence = fake_gather
    nodes.rewrite_engine.pick_artifacts = lambda *a, **k: ["faq_block"]
    nodes.rewrite_engine.generate_artifact = fake_generate_artifact


# --- savers -------------------------------------------------------------

def _saver_cm(kind: str, sqlite_path: str | None):
    if kind == "postgres":
        from app.graph.checkpointer import build_checkpointer

        return build_checkpointer()
    if kind == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        if not sqlite_path:
            raise SystemExit("--sqlite-path is required with --saver sqlite")
        return AsyncSqliteSaver.from_conn_string(sqlite_path)
    raise SystemExit(f"unknown saver {kind!r}")


async def _main(args: argparse.Namespace) -> int:
    _install_fakes(args.audit_id)
    from app.graph.loop_graph import (
        build_graph,
        get_pending_review,
        initial_state,
        resume_graph_cycle,
    )

    async with _saver_cm(args.saver, args.sqlite_path) as saver:
        await saver.setup()

        if args.phase == "start":
            graph = build_graph(saver)
            out = await graph.ainvoke(
                initial_state(args.audit_id),
                config={"configurable": {"thread_id": args.audit_id}},
            )
            if "__interrupt__" not in out:
                print("UNEXPECTED: graph did not pause at the review gate")
                return 1
            print(f"PAUSED thread={args.audit_id} saver={args.saver}")
            print("  the pause is now in the checkpointer; this process will exit")
            return 0

        pending = await get_pending_review(args.audit_id, checkpointer=saver)
        if pending is None:
            print(
                f"NOT PAUSED: nothing awaiting review for thread={args.audit_id}.\n"
                "  Either phase 'start' was not run, or it used a different saver/thread."
            )
            return 1
        print(f"FOUND PAUSE in a fresh process: thread={args.audit_id}")
        print(f"  awaiting={pending['awaiting']} next={pending['next_nodes']}")
        artifacts = (pending.get("payload") or {}).get("artifacts") or []
        print(f"  artifacts awaiting review: {len(artifacts)}")

        out = await resume_graph_cycle(
            args.audit_id,
            {"decision": args.decision, "published": args.published,
             "reviewer": "verify_graph_pause.py", "notes": "restart proof"},
            checkpointer=saver,
        )
        print(
            f"RESUMED decision={out.get('decision')!r} "
            f"reason={out.get('decision_reason')!r} retest={out.get('retest')}"
        )
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", choices=("start", "resume"), required=True)
    p.add_argument("--audit-id", default=str(uuid.uuid4()))
    p.add_argument("--saver", choices=("postgres", "sqlite"), default="postgres")
    p.add_argument("--sqlite-path", default=None)
    p.add_argument("--decision", choices=("approve", "revise", "reject"), default="reject")
    p.add_argument("--published", action="store_true")
    args = p.parse_args()
    try:
        return asyncio.run(_main(args))
    except Exception as exc:  # noqa: BLE001 - this is a CLI, print don't traceback
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
