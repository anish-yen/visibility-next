"""
Audit pipeline: real crawl → Gemini prompts → simulated visibility (Gemini) → gaps → recommendations.
Runs as an asyncio task after POST /audits. In-memory state only (no Celery) for MVP.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from app import audit_store
from app.services.crawler import crawl_site
from app.services.gemini_client import require_gemini_api_key
from app.services.prompt_generator import generate_audit_prompts
from app.services.recommendations_builder import build_recommendations
from app.services.visibility_evaluator import simulate_answer_and_score

logger = logging.getLogger(__name__)


def _label_for_crawl_index(state: audit_store.AuditState, index: int) -> str:
    if index == 0:
        return state.primary_domain
    if index - 1 < len(state.competitor_domains):
        return state.competitor_domains[index - 1]
    return f"index_{index}"


def _competitor_scores(
    primary_domain: str,
    competitors: list[str],
    prompt_rows: list[dict[str, Any]],
    visibility: float,
) -> list[dict[str, Any]]:
    """Bar-chart style scores: You = audit visibility; competitors = mention rate in simulated answers."""
    out: list[dict[str, Any]] = [
        {
            "domain": primary_domain,
            "score": round(min(100.0, max(0.0, visibility)), 1),
            "label": "You",
        }
    ]
    n = max(1, len(prompt_rows))
    for d in competitors:
        key = d.split(".")[0].lower()
        hits = 0
        for p in prompt_rows:
            ra = (p.get("raw_answer") or "").lower()
            if d.lower() in ra or key in ra:
                hits += 1
                continue
            for m in p.get("competitor_mentions") or []:
                ml = str(m).lower()
                if d.lower() in ml or key in ml:
                    hits += 1
                    break
        rate = min(100.0, 100.0 * hits / n)
        part = d.split(".")[0].replace("-", " ").title()
        out.append({"domain": d, "score": round(rate, 1), "label": part})
    return out


async def run_audit_pipeline(audit_id: str) -> None:
    state = audit_store.get(audit_id)
    if not state:
        logger.error("run_audit_pipeline: missing audit %s", audit_id)
        return

    try:
        require_gemini_api_key()
    except RuntimeError as e:
        logger.error("audit=%s aborted: %s", audit_id, e)
        audit_store.fail_audit(audit_id, str(e))
        return

    try:
        logger.info("audit=%s stage=crawling", audit_id)
        audit_store.update_progress(audit_id, stage="crawling", progress_percent=8)

        crawl_tasks = [crawl_site(state.primary_domain)]
        for c in state.competitor_domains:
            crawl_tasks.append(crawl_site(c))

        results = await asyncio.gather(*crawl_tasks, return_exceptions=True)
        all_pages: list[dict[str, Any]] = []
        for i, res in enumerate(results):
            label = _label_for_crawl_index(state, i)
            if isinstance(res, Exception):
                logger.error("Crawl failed for %s: %s", label, res)
                continue
            all_pages.extend(res)

        state.crawled_pages = all_pages
        logger.info("audit=%s crawl done pages=%s", audit_id, len(all_pages))

        audit_store.update_progress(audit_id, stage="generating_prompts", progress_percent=25)

        try:
            generated = await asyncio.to_thread(
                partial(
                    generate_audit_prompts,
                    primary_domain=state.primary_domain,
                    competitor_domains=state.competitor_domains,
                    industry=state.industry,
                    crawled_pages=state.crawled_pages,
                    count=15,
                ),
            )
        except Exception as e:
            logger.exception("audit=%s prompt generation failed", audit_id)
            audit_store.fail_audit(
                audit_id,
                f"Prompt generation failed: {e}",
            )
            return

        if not generated:
            audit_store.fail_audit(audit_id, "No prompts generated.")
            return

        logger.info("audit=%s stage=evaluating prompts=%s", audit_id, len(generated))
        audit_store.update_progress(audit_id, stage="evaluating", progress_percent=40)

        prompt_rows: list[dict[str, Any]] = []
        total = len(generated)
        successes = 0

        for i, row in enumerate(generated):
            try:
                ev = await asyncio.to_thread(
                    partial(
                        simulate_answer_and_score,
                        prompt_text=row["text"],
                        target_domain=state.primary_domain,
                        competitor_domains=state.competitor_domains,
                        crawled_pages=state.crawled_pages,
                    ),
                )
                successes += 1
                prompt_rows.append(
                    {
                        "id": row["id"],
                        "text": row["text"],
                        "intent": row.get("intent", "informational"),
                        "mentioned": ev["mentioned"],
                        "score": float(ev["score"]),
                        "competitor_mentions": ev["competitor_mentions"],
                        "answer_summary": ev["answer_summary"],
                        "raw_answer": ev["raw_answer"],
                    }
                )
            except Exception as e:
                logger.warning(
                    "audit=%s eval failed for prompt %s: %s",
                    audit_id,
                    row.get("id"),
                    e,
                )
                prompt_rows.append(
                    {
                        "id": row["id"],
                        "text": row["text"],
                        "intent": row.get("intent", "informational"),
                        "mentioned": False,
                        "score": 0.0,
                        "competitor_mentions": [],
                        "answer_summary": "",
                        "raw_answer": "",
                    }
                )

            pct = 40 + int(45 * (i + 1) / max(1, total))
            audit_store.update_progress(audit_id, stage="evaluating", progress_percent=min(pct, 88))

        visibility = (
            100.0
            * sum(float(p["score"]) for p in prompt_rows)
            / max(1, len(prompt_rows))
        )
        visibility = float(min(100.0, max(0.0, round(visibility, 1))))

        logger.info(
            "audit=%s stage=analyzing visibility=%s eval_ok=%s/%s",
            audit_id,
            visibility,
            successes,
            total,
        )
        audit_store.update_progress(audit_id, stage="analyzing", progress_percent=92)

        comp_scores = _competitor_scores(
            state.primary_domain,
            state.competitor_domains,
            prompt_rows,
            visibility,
        )
        recommendations = build_recommendations(
            audit_id=audit_id,
            primary_domain=state.primary_domain,
            crawled_pages=state.crawled_pages,
            prompt_rows=prompt_rows,
            industry=state.industry,
        )

        dashboard_prompts = [
            {k: v for k, v in p.items() if k != "raw_answer"} for p in prompt_rows
        ]

        audit_store.complete_audit(
            audit_id,
            visibility_score=visibility,
            competitor_scores=comp_scores,
            prompts=dashboard_prompts,
            recommendations=recommendations,
        )
        logger.info("audit=%s stage=completed", audit_id)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("audit=%s pipeline error", audit_id)
        audit_store.fail_audit(audit_id, str(e))
