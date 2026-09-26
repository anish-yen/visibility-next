# Agentic Visibility Loop

Five new modules turn the one-pass audit into a loop that tests real answer
engines, reads what they cite, rewrites the client's content from that
evidence, and re-tests until it plateaus.

## New files

| File | Role |
| --- | --- |
| `app/services/answer_engines.py` | Runs buyer prompts against real engines and captures cited URLs. Free default: Gemini + Google Search grounding (existing `GEMINI_API_KEY`). Stubs for browser-driven ChatGPT/Perplexity web UIs. Optional paid upgrades: Perplexity Sonar, OpenAI + web_search (env-gated). |
| `app/services/citations.py` | Aggregates citations into a citation map: which domains the engines read, source types, owned-citation share. |
| `app/services/tech_checks.py` | Machine-readability checks: llms.txt, homepage JSON-LD schema, entity clarity, high-intent page coverage. |
| `app/services/rewrite_engine.py` | Evidence-driven rewrites. Fetches the pages engines actually cite, dissects their structure, rewrites the client's content with verified facts only; unknowns become `[CONFIRM: ...]` placeholders. |
| `app/loop_runner.py` | `run_cycle()`: crawl -> stable prompt set -> real engines -> citation map -> tech checks -> rewrites -> snapshot -> lift + stopping rule (target mention rate 0.60, plateau < 3 pts over 2 cycles, max 6 cycles). |

## Setup

1. `backend/.env`: set `GEMINI_MODEL=gemini-2.0-flash` (grounding needs a 2.x model).
2. Run `backend/supabase/migrations/visibility_cycles.sql` in the Supabase SQL editor.
3. Snapshot persistence: `app/cycle_store.py`. `get_cycle_store()` returns the
   Supabase store when `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set, else a
   shared in-memory store (local dev/tests). Both implement save/latest/list/get.

## Wired

- `POST /audits/{id}/cycle` (202): owner-only, runs `loop_runner.run_cycle` as a
  background task with `cycle_store.get_cycle_store()`.
- `GET /audits/{id}/cycles`: cycle summaries (newest first) for the frontend.
- `GET /audits/{id}/cycles/{n}`: one full snapshot (prompt results, citation map,
  tech checks, rewrite artifacts).
- Snapshots persist lift/decision at save time, so the plateau rule reads real
  history across restarts.

Tests: `backend/tests/` (13 tests; `pip install -r requirements-dev.txt`,
`python -m pytest tests/`).

## Later upgrades (not blockers)

- Browser runners for ChatGPT/Perplexity web lanes (stubs included).
- `PERPLEXITY_API_KEY` / `OPENAI_API_KEY` for the paid API lanes.
- Scheduled re-runs (e.g. daily cron calling `run_cycle` for active clients).
