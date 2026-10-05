# Zeteum — Plan of Action (`agentic-visibility-loop`)

Branch: `agentic-visibility-loop` @ `c725a98`
Scope: fix measurement trust and safety first, then make one complete product, then build the real improvement workflow, then validate.

> Line numbers below are as of `c725a98` and will drift as you edit. Re-grep before each change.
> Golden rule for every task: **every number a user sees must be traceable to an answer or a citation we actually captured.**

---

## Phase 0 — Consolidate and set a baseline

The repo currently describes three different products (PRD in `CLAUDE.md`, `main`, and this branch). Make this branch the single source of truth before changing behavior.

- [ ] **0.1 Declare this branch canonical.** Confirm `agentic-visibility-loop` is ahead of `main` by the 19 feature commits and that `main` only holds the earlier merge. Decide the merge-back strategy now (squash into `main` after Phase 1, or keep developing here). Record the decision at the top of `AGENTIC_LOOP.md`.
- [ ] **0.2 Add developer gates that do not exist yet.** There is no CI and no frontend test runner.
  - Add a GitHub Actions workflow: `backend` → `pip install -r requirements.txt -r requirements-dev.txt` + `pytest`; `frontend` → `npm ci` + `npm run lint` + `npm run build`.
  - Acceptance: a PR against this branch runs backend tests and a frontend build automatically.
- [ ] **0.3 Pin the model defaults.** `backend/app/config.py` defaults `GEMINI_MODEL` to `gemini-1.5-flash`, but grounded answers in `answer_engines.run_gemini_grounded` default to `gemini-2.0-flash`. Pick one default (2.0-flash, since grounding needs a 2.x model) and use it everywhere so audit scores and cycle scores are produced by the same model.
  - Acceptance: one model constant/env resolves identically in `config.py` and `answer_engines.py`.

---

## Phase 1 — Trust and safety (do this before any demo to a paying user)

### 1.1 Make brand-vs-competitor scores a fair, symmetric comparison
**Problem:** target and competitors are scored with different formulas, so a competitor can outscore the audited company without ever being mentioned.
**Evidence:** `backend/app/audit_pipeline.py`
- Target score: lines `1034–1042` (`avg_prompt_score*0.55 + mention_rate*0.20 + weighted_bucket*0.25`).
- Competitor score: lines `1057–1073` — `mention_count/total*0.65 + page_strength(0.45) + comparison_bonus(0.15) + review_bonus(0.10)`.
- Verified: with **zero brand mentions in any answer**, a competitor with pages + comparison + reviews returns **70.0** while the target returns **0**.

Steps:
- [ ] **1.1.a** Define one visibility metric computed identically for the target and every competitor: mention rate (and, once 1.3 lands, citation share) over the **same prompt subset and denominator**.
- [ ] **1.1.b** Remove the page/comparison/review bonuses from the competitor *visibility* number (lines `1064–1066`). Content completeness is a diagnostic, not visibility — surface it in a separate `content_readiness` field, never folded into the comparison bar.
- [ ] **1.1.c** Fix the incomplete unbranded split: `total_prompts` is the unbranded count (line `989`) but competitor `mention_count` still spans **all** prompts (lines `1059–1062`). Restrict `mention_count` to the same `headline` list so numerator and denominator agree.
- [ ] **1.1.d** Acceptance + test: add `backend/tests/test_scoring.py`.
  - No tracked brand mentioned anywhere ⇒ every entity (target and competitors) scores 0 on the visibility metric.
  - A competitor mentioned only in a branded (excluded) prompt does **not** reach a high visibility score.
  - Target and a competitor with identical mention patterns score identically.

### 1.2 Stop common words from counting as brand mentions
**Problem:** alias extraction and substring matching produce false-positive mentions.
**Evidence:** `backend/app/audit_pipeline.py`
- `_brand_variants` lines `756–783`; `first_word` extraction `780–782` turns title "Welcome to Acme" into alias `welcome` (verified).
- `_find_brand` substring scan `786–797`.
- `backend/app/services/answer_engines.py:64–75` `detect_mentions` does raw `label_l in h` substring matching; short labels ("Box", "Front", "Base") match normal prose.

Steps:
- [ ] **1.2.a** Drop the generic first-word/title-token aliasing (lines `780–782`). Build aliases only from the registered domain root and an explicit brand label, plus a short curated allowlist per audit if needed.
- [ ] **1.2.b** Add a stopword/common-English guard so a single dictionary word is never a standalone alias.
- [ ] **1.2.c** Use whole-token/word-boundary matching in both `_find_brand` and `detect_mentions`, not bare substring (`_find_brand` already checks boundaries — make `detect_mentions` match it, and apply it to cited URLs by host, not substring).
- [ ] **1.2.d** Acceptance + test: "Welcome to this guide…" is **not** a mention of `welcome.io`; "use Box for storage" **is** a mention of `box.com`; a competitor substring inside a larger word is not a hit.

### 1.3 Make citations trustworthy (this is the product's strongest idea)
**Problem:** competitors are treated as "owned", and the actual cited page paths are thrown away.
**Evidence:**
- `backend/app/loop_runner.py:161–163` passes `owned_domains={state.primary_domain, *comp_domains}` — competitors flagged owned.
- `backend/app/services/citations.py:70–90` — `winning_sources` key by domain only (URL path discarded); `owned_citation_share` (lines `88–90`) sums competitors. Verified: a rival-only citation yields `owned_citation_share = 1.0`.
- `backend/app/services/rewrite_engine.py:49–64` — skips `is_owned` (so competitors are never studied) and refetches `https://{domain}` (line ~`57`), i.e. the homepage, not the cited page.

Steps:
- [ ] **1.3.a** Classify every citation as **target**, **competitor**, or **third-party** separately. `owned_citation_share` must count only the target's own domain.
- [ ] **1.3.b** Preserve the full cited URL (and the prompt it came from) through `build_citation_map`, not just the host.
- [ ] **1.3.c** In `gather_winning_evidence`, fetch the **actual cited pages** (target-excluded, competitor-included) and keep their link to the losing prompts.
- [ ] **1.3.d** Acceptance + test: `backend/tests/test_citations.py` — rival-only citation ⇒ `owned_citation_share == 0.0`, rival classified `competitor`; evidence fetch targets the exact cited URL, not the homepage.

### 1.4 Be honest about what the number means
**Problem:** the scalar surfaced to users looks like a measurement of ChatGPT/Perplexity; the normal audit is a Gemini-API answer, and `main`-style self-grading silently falls back to heuristics.
**Evidence:** `_evaluate_prompt` `audit_pipeline.py:800–899` (this branch answers then grades without injecting site context — keep that improvement); grounded real-engine runners in `answer_engines.py` are only reachable through the cycle path, never from `run_audit` (`1470–1618`).

Steps:
- [ ] **1.4.a** Stamp every result with an explicit `measurement_source` (`gemini_answer`, `gemini_grounded`, `heuristic_fallback`) and the model/engine used, and persist it.
- [ ] **1.4.b** Keep separate, clearly-labeled metrics: **mention rate**, **citation share**, **recommendation prominence**, **content readiness**. Do not blend them into one unexplained 0–100.
- [ ] **1.4.c** Ensure a degraded/heuristic run is visibly labeled degraded in the API payload and UI, never shown as a clean score.
- [ ] **1.4.d** Acceptance: product copy and the dashboard say exactly what was measured ("Gemini + Google Search grounding", not "ChatGPT").

### 1.5 Close the SSRF / unsafe-fetch hole (blocker for public launch)
**Problem:** the crawler will fetch user-supplied internal/loopback/metadata hosts and follows redirects with no public-address guard.
**Evidence:** `backend/app/schemas_audit.py:4–14` + `backend/app/audit_store.py:37–43` accept `127.0.0.1:8000` and `169.254.169.254` (verified); `backend/app/services/crawler.py` builds `https://{normalized}` (`595–607`), `_fetch_html` follows redirects (`355–365`), `_can_fetch` returns `True` on any error/4xx (`341–352`).

Steps:
- [ ] **1.5.a** Validate the submitted domain: public DNS name only; reject IP literals, loopback, link-local, RFC1918, and metadata ranges **after** DNS resolution.
- [ ] **1.5.b** Re-validate on every redirect hop (resolve-then-check), and cap redirects.
- [ ] **1.5.c** Add a response body-size cap and total per-crawl byte/time budget.
- [ ] **1.5.d** Acceptance + test: `backend/tests/test_crawler_safety.py` — loopback, `169.254.169.254`, and a public→internal redirect are all refused before any socket to the internal host.

---

## Phase 2 — One complete, durable product

### 2.1 Durable, out-of-process audit jobs
**Problem:** audit work runs as an in-process task; completed prompt evaluations are not checkpointed, so a restart mid-audit loses progress even though this branch persists `AuditState` to Supabase.
**Evidence:** `backend/app/routers/audits.py` `create_audit` fires `asyncio.create_task(run_audit(...))`; `run_audit` (`audit_pipeline.py:1470–1618`) writes results only at the end.

Steps:
- [ ] **2.1.a** Persist each prompt result as it completes (checkpoint), so a resumed audit continues instead of restarting.
- [ ] **2.1.b** Move audit execution to a worker the API process restart cannot kill (the PRD already names Celery + Redis; a minimal task runner is acceptable, but it must survive an API redeploy).
- [ ] **2.1.c** Keep the `asyncio.create_task` reference or hand off to the worker so the job cannot be garbage-collected.
- [ ] **2.1.d** Acceptance: killing/restarting the API mid-audit leaves the audit resumable, not stuck at `running` forever.

### 2.2 Per-user quotas and abuse limits
**Problem:** `POST /audits` and `POST /audits/{id}/cycle` have no quota; each burns real crawl + Gemini quota.
**Evidence:** `routers/audits.py` create/cycle endpoints have no rate check; no rate-limit middleware anywhere.

Steps:
- [ ] **2.2.a** Enforce the tier limits the PRD already defines (audits/week, prompts/audit, briefs) per user.
- [ ] **2.2.b** Add a basic per-user rate limit on the heavy endpoints.
- [ ] **2.2.c** Tighten CORS: `main.py:15–22` allows any `*.vercel.app` with credentials — pin to your actual deployment origins.
- [ ] **2.2.d** Acceptance: exceeding a tier limit returns a clear 429/402-style error without starting work.

### 2.3 Expose the evidence the backend already produces
**Problem:** the dashboard drops the raw answers and never shows citations, cycles, or history.
**Evidence:** `backend/app/schemas_audit.py:23–31` (`PromptRowOut`) omits `answer`/`evaluation_source`; `routers/audits.py:36–58` `_to_detail` never includes them; `frontend/src/components/dashboard/DashboardFlow.tsx:98` shows only `list[0]`.

Steps:
- [ ] **2.3.a** Add `answer`, `measurement_source`, and cited URLs to `PromptRowOut` and `_to_detail`.
- [ ] **2.3.b** Build an audit-history list + selector (not just the latest), plus the citation map and per-prompt answer/sources in the UI.
- [ ] **2.3.c** Render the real score breakdown (`score_components` is already fetched and stored but never displayed) so a user can see what dragged the number down. Add a one-line scale explanation to the prompt score column.
- [ ] **2.3.d** Acceptance: for any prompt a user can read the answer we evaluated and the sources it cited.

### 2.4 Fix the user-flow correctness gaps
**Evidence:** `DashboardFlow.tsx` — running view renders blank while the first `getAudit()` resolves (`~352`, no null branch); boot error has no retry (`249–254`); five form inputs lack `id`/`htmlFor` (`272–348`); fonts are loaded then overridden.

Steps:
- [ ] **2.4.a** Add loading/empty states for the running view and a retry control on boot error.
- [ ] **2.4.b** Associate every `<label>` with its input (`htmlFor`/`id`); fix the font override so the loaded Geist fonts are actually applied (or stop shipping them).
- [ ] **2.4.c** Pre-populate domain/competitors on "re-audit" so the loop does not start from a blank form.
- [ ] **2.4.d** Acceptance: keyboard/screen-reader users can complete the form; no blank screens on normal transitions.

### 2.5 Align the landing page with reality
**Problem:** `frontend/src/app/page.tsx` (this branch) promises ChatGPT results, "every answer," and "the exact sources it cited" — the normal audit does not yet deliver that.
- [ ] **2.5.a** Either deliver those (depends on 1.3/1.4/2.3) or soften the copy to what Gemini-grounded measurement actually provides.
- [ ] **2.5.b** Acceptance: no public claim exceeds what a completed audit shows.

---

## Phase 3 — The real improvement loop (human-in-the-loop)

The current loop generates rewrite drafts, saves them, and re-crawls the live site; it does not apply drafts or test them. That is fine (the PRD forbids auto-publishing), but the surrounding workflow is missing.

### 3.1 Enforce the stop decision and prevent concurrent cycles
**Evidence:** `backend/app/routers/audits.py:151–160` adds the cycle task unconditionally; `loop_runner.run_cycle` (`104–231`) stores `decision="stop"` but nothing reads `previous["decision"]`; `cycle_store.py:120–133` can hit the `UNIQUE(audit_id, cycle_number)` constraint when two cycles race, and `BackgroundTasks` swallows the failure.

Steps:
- [ ] **3.1.a** Refuse a new cycle when the latest snapshot's `decision == "stop"` (allow an explicit override flag).
- [ ] **3.1.b** Serialize cycles per audit (advisory lock / status flag) so two requests cannot compute the same `cycle_number`.
- [ ] **3.1.c** Surface background-task failures instead of returning `202` for a cycle that silently failed to persist.
- [ ] **3.1.d** Acceptance + test: repeated POSTs after a stop do not burn API quota; concurrent cycle requests produce one coherent snapshot or a clear error.

### 3.2 Make the cycle metric match the per-prompt metric
**Problem:** per-prompt `score` is a continuous engine mention rate, but the cycle mention rate counts a prompt as fully mentioned if **any** engine mentioned the brand.
**Evidence:** `loop_runner._evaluate_with_real_engines` (`57–102`, `score = round(mention_rate, 2)`, `mentioned = mentioned > 0`); `run_cycle` aggregates boolean `mentioned` (`196–199`). Verified: 1 of 2 engines ⇒ `score 0.5` but counts as a full mention.
- [ ] **3.2.a** Use one consistent definition for the stopping metric (either continuous engine rate everywhere, or boolean everywhere) and document it.
- [ ] **3.2.b** Version the engine set in each snapshot so lift is only compared across identical engine sets.
- [ ] **3.2.c** Acceptance + test: stopping metric and per-prompt metric agree for the single-engine default; mixed-engine cycles are not compared against single-engine baselines.

### 3.3 Build the approve → deploy → re-test → compare workflow
- [ ] **3.3.a** UI to review each rewrite artifact (with its `[CONFIRM: …]` placeholders) and mark it approved/deployed, recording what changed and when.
- [ ] **3.3.b** Re-run the **stable prompt set** after a deploy and show a before/after comparison with the evidence (answers + citations) behind any lift.
- [ ] **3.3.c** Respect robots.txt when fetching competitor/winning evidence pages (the client's own crawl checks robots; evidence fetch in `rewrite_engine.py` does not).
- [ ] **3.3.d** Acceptance: "we changed X, and these measured outcomes changed" is backed by stored, inspectable evidence.

---

## Phase 4 — Validation and expansion (only after Phases 1–3)

- [ ] **4.1 Consistency sampling.** Run each prompt 2–3 times and report agreement/variance (the PRD targets ≥80% agreement on mention presence). Surface a confidence indicator.
- [ ] **4.2 Calibration benchmark.** Hand-label a small set of real queries/brands and measure how often the scorer agrees with a human. This is how you learn whether the number is useful at all.
- [ ] **4.3 Additional engine lanes.** Implement the ChatGPT/Perplexity web runners (currently stubs in `answer_engines.py:148–168`) or wire the paid API lanes — only once the single-engine metric is trustworthy. Note: engines are not auto-selected by keys alone today; `DEFAULT_ENGINES=("gemini",)`.
- [ ] **4.4 Reconcile the PRD.** `CLAUDE.md` promises 25–100 prompts, repeated sampling, richer history, and a queue architecture the code does not implement. Update the spec to match, or schedule the work.
- [ ] **4.5 Narrow pilot.** Run 3–5 real customers through the full cycle (audit → review → deploy → re-measure) and confirm the recommendations were worth their time before broad/paid release.

---

## Suggested execution order

1. **1.1, 1.2, 1.3, 1.4** — measurement trust (ship nothing with the old competitor formula).
2. **1.5** — safety gate before any public URL intake.
3. **2.1, 2.2** — durability + quotas so real traffic cannot wedge or drain the system.
4. **2.3, 2.4, 2.5** — let users see and trust the evidence.
5. **3.1, 3.2, 3.3** — the actual optimization loop.
6. **Phase 4** — validate, then expand.

Do not add more engines, agents, or services before Phases 1–3 make the existing output trustworthy and complete.
