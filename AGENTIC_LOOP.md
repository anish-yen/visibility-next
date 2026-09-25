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
3. Implement the `SnapshotStore` protocol from `loop_runner.py` (~20 lines over the
   existing Supabase admin client: `save()` inserts a row, `latest()` selects the
   newest by `cycle_number`).

## Wiring (one endpoint, ~30 lines)

```python
# app/routers/audits.py
from app import loop_runner

@router.post("/audits/{audit_id}/cycle", response_model=dict)
async def run_cycle_endpoint(request: Request, audit_id: str, background_tasks: BackgroundTasks):
    audit = audit_store.get(audit_id)
    _require_owner(request, audit)
    background_tasks.add_task(loop_runner.run_cycle, audit_id, SupabaseSnapshotStore())
    return {"status": "running", "audit_id": audit_id}
```

Then `GET /audits/{id}/cycles` reads the snapshots back for the frontend.

## Later upgrades (not blockers)

- Browser runners for ChatGPT/Perplexity web lanes (stubs included).
- `PERPLEXITY_API_KEY` / `OPENAI_API_KEY` for the paid API lanes.
- Scheduled re-runs (e.g. daily cron calling `run_cycle` for active clients).
