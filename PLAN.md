# Zeteum: standing product and execution plan

Updated: October 7, 2026. Owner: Anish Yenduri.
Code inspected: `main` at `2cac8b4` before this documentation change.

Read this file before planning Zeteum work. It separates intent, code present, dated operational evidence and work not yet verified. It supersedes older status/priority claims in `CLAUDE.md`, `AGENTIC_LOOP.md` and `PLAN_OF_ACTION.md`; those files remain useful design and engineering notes, not a current completion checklist. This file contains no credentials.

## Product and purpose

Zeteum helps small businesses and SaaS founders see whether AI answer engines name them when real buyers ask what to buy, understand which sources and competitors appear, and produce specific content changes with evidence. The product should give founders useful copy/diffs and a repeatable before/after, not just a dashboard number.

Inputs: the business website, competitors and optional category/customer context. Outputs: natural buyer prompts, engine-specific answers and citations, target/competitor mention metrics, content gaps, prioritized recommendations, content briefs and proposed rewrites. Track results over time only when storage is verified durable.

The initial commercial experiment was a done-for-you service priced at $149, later $249. Those are historical proposals, not verified current checkout prices or refund commitments. The self-serve product's tiers and limits in the original PRD are roadmap hypotheses. No first sale is verified here.

## Current priorities

The latest working-plan block in Anish's project notes sets this order:

1. Resolve the audit-table decision, apply the approved migration, deploy to staging and run one full end-to-end audit. `visibility_audits` stores audits; `visibility_cycles` is separate. A migration file in Git does not prove the live table exists. **Blocked: needs Anish's explicit go-ahead before anyone touches the live database.**
2. ~~Fix competitor scoring before showing public audits.~~ **Done, branch `fix/competitor-scoring-symmetry`, not yet merged.** See "Dated operational evidence" below for what was verified.
3. Run 3-5 free audits on genuine small brands, not only Stripe/PayPal/Shopify. Preserve raw prompts, answers, citations, dates and failure records. Draft Reddit/X posts for Anish's review.
4. Cold-email sending stays parked until those audits/posts show traction. Existing reply and permanent-bounce monitoring is separate from new outreach. Do not interpret old scheduled-wave notes as permission to send.
5. Capture an audited site's actual typography, colors, spacing and copy voice in `style.md`, with do/don't examples, before asking a rewrite model to match it. This was proposed, not verified completed. Do not impose Zeteum's own style on every customer's website.

The older detailed remediation backlog is in `PLAN_OF_ACTION.md`. Its branch/head assumptions are stale. Re-check each issue against current code before implementing it. It also flags citation ownership/full-URL handling, brand-alias false positives, unsafe fetch/SSRF protection, worker durability, quotas, UI evidence exposure and loop concurrency/stop enforcement. These are launch-readiness checks, not completed fixes.

## Build status: code present is not runtime proof

Source: fresh inspection of `main` on October 7.

- Frontend: Next.js/React/Tailwind, Supabase-auth integration and dashboard code are present. FastAPI exposes owner-scoped audit, brief and cycle endpoints.
- Ordinary `POST /audits` starts an in-process task. Its Gemini evaluation asks buyer questions from model knowledge; it is NOT the same as querying consumer ChatGPT or a search-grounded engine.
- The separate cycle path includes Gemini + Google Search grounding, citation analysis, technical checks, evidence-driven rewrite drafts, stable prompt reuse, snapshots and lift/stop-decision code. A stored stop decision is not proof endpoint enforcement or a complete autonomous loop works.
- `backend/app/services/answer_engines.py` already has optional Perplexity Sonar and OpenAI search runners. Sonar defaults to the `sonar` model and is credential-gated. The default cycle engine set is Gemini only. Sonar activation, credentials, billing and a successful production run are not verified.
- Consumer ChatGPT/Perplexity web runners are stubs in code. Past manual logged-out screenshot tests are separate evidence, not implemented automatic lanes.
- Supabase audit/cycle adapters and migration files exist. Audit storage falls back to memory when `visibility_audits` is missing, so those audits are lost on restart. The last engineering handoff reported that table missing; current database state has not been rechecked for this file. Do not claim durable history until table, write/read and restart checks pass.
- Competitor visibility scoring fixed on branch `fix/competitor-scoring-symmetry` (not yet merged): competitors are now scored with the exact same per-prompt formula, weights and unbranded-prompt subset/denominator as the target (`_score_entity_against_rivals` in `audit_pipeline.py`, reusing `_compute_prompt_score`). Page/comparison/review completeness is reported separately as `content_readiness` on each entity and no longer blended into the visibility score. Three new unit tests in `backend/tests/test_scoring.py` cover: zero mentions scores zero for every entity regardless of content completeness (the literal reported bug), a competitor scores identically to the target under a symmetric mention pattern, and a mention on a branded-only prompt does not count toward a competitor's score. All 19 backend tests pass. Live-verified locally (not staging) with a real cal.com vs. calendly.com audit on real Gemini answers: a competitor mentioned centrally in every answer scored near 100 (legitimate, not the bug), the target scored 0 when never mentioned.
- Branded-prompt scoring leak fixed on branch `fix/branded-prompt-filtering` (stacked on the branch above, not yet merged): `names_brand` only checked the target's own name, so a prompt naming only a competitor (e.g. "best alternatives to jira") silently counted as unbranded and could inflate the headline score. Prompts are now tagged branded if they name the target OR any competitor (`_tag_names_brand`), capped at 2 branded prompts in the final set, and the unbranded subset must clear a minimum count or generation is retried (`_finalize_prompt_set`, `BRANDED_PROMPT_CAP`, `MIN_UNBRANDED_PROMPTS`). Branded prompts are shown in the dashboard in a separate "reference only" table, explicitly excluded from the score. Also fixed in the same branch: (1) generated prompts read like SEO keyword stacks ("what tool do fast-growing software startups use instead of jira for roadmap planning") rather than real buyer queries - rewrote the Gemini system/user prompts toward short, casual phrasing and added a `_looks_like_real_buyer_query` heuristic filter (rejects long unbroken runs of content words with no connecting function word); (2) the "weak" bucket label was unconditional - `_format_rationale` said "Informational prompts are weak (0.96)" even when 0.96 is a strong score, because recommendations can trigger on a missing page type alone, not just a low score. Rationale text now only says "weak" below `WEAK_BUCKET_THRESHOLD` (0.65, matched in the frontend's `weakBucketChips`); (3) per-prompt explanation text always repeated "target mentioned first among tracked brands" - now picks a deterministic-but-varied phrase per prompt id (`_mention_explanation`). 7 new tests in `backend/tests/test_prompt_quality.py`, 2 more in `test_scoring.py`; all 28 backend tests pass; frontend typechecks clean.

### Dated operational evidence

October 5: PR #4 merged into main at `2d6c3c9`; Vercel was connected to `anish-yen/visibility-next`, root directory set to `frontend`, and that revision deployed. The login page was visually checked for the new restrained white/indigo design. A temporary deployment hook was removed after use.

October 7: `/login` returned Zeteum content. A fetch of backend `/health` returned 503. One failed fetch is not a diagnosis of an outage; backend health and authenticated end-to-end operation remain unverified. A real login and future automatic deployments also need testing.

October 7 (local dev, not staging/production): `backend/.env`'s `GEMINI_MODEL` was pinned to `gemini-2.0-flash`, which Google has sunset (API returns 404 "This model ... is no longer available" on every call). This broke prompt generation and evaluation for every local audit. Confirmed via a direct call to `GET https://generativelanguage.googleapis.com/v1beta/models`; `gemini-2.5-flash` works for both plain `generateContent` and the `google_search` grounding tool. Fixed locally in `backend/.env` (not committed, gitignored) and updated the non-secret `.env.example` default so fresh setups don't hit the same wall. Production/staging env vars were not touched or checked.

October 7 (local dev): separately, prompt generation intermittently fails with "Prompt generation returned too few usable prompts" after 3 retries (distinct from the model-sunset issue above; this is Gemini returning fewer than `PROMPT_TARGET_MIN` prompts after sanitization). Observed twice, succeeded on retry both times. Not investigated further and not part of the scoring fix; flagging as an open flakiness item, not reproduced to a root cause.

October 7 (local dev): the `gemini-2.5-flash` free-tier key hit a hard daily cap mid-session - `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit 20/day, confirmed via the API's own error body (`quotaValue: "20"`, ~3h16m retry delay quoted). 20/day cannot complete even one real audit (each needs roughly 10-14 Gemini calls), let alone the planned 6-vertical sanity sweep. With Anish's approval, `GEMINI_MODEL` was switched locally (not committed; `.env` is gitignored) to `gemini-flash-lite-latest`, which had separate, unexhausted quota, for the remainder of this session's testing only. The committed `.env.example` default stays `gemini-2.5-flash`. Anyone resuming local testing should check `GET https://generativelanguage.googleapis.com/v1beta/models?key=...` for 429s before assuming the scoring/generation logic is at fault - this cost real time during this session before the quota message was read.

October 7 (local dev, on `gemini-flash-lite-latest` because of the quota issue above): ran linear.app vs. atlassian.com as a one-off sanity check (separate from, and prior to, the systematic 6-vertical sweep Anish later asked for - that sweep did not complete; see below). First run, before the branded-prompt fix: Linear scored 94/100, Atlassian scored ~0. Investigation found two compounding problems, not one: (a) `names_brand` only checked the target's name, so 6 of 9 "buyer prompts" the generator produced literally said "linear" or "jira vs linear" - they were closer to branded reputation questions than blind buyer intent, which is exactly what inflated the score; (b) Atlassian's near-0 score traced to a real, separate crawl limitation: the competitor's `label`/brand variants are derived from the domain (`atlassian.com` -> "Atlassian"), but a direct crawl of `atlassian.com` (verified live) never reaches a page that says "Jira" - only the homepage and customer/review pages were fetched, and Atlassian's own homepage title doesn't mention Jira either (it's a multi-product portfolio page). So when a real Gemini answer said "Jira" instead of "Atlassian," no mention was detected. This is a brand-identity/crawl-coverage limitation, not a scoring-formula bug - the unit tests confirm the formula correctly counts mentions when a competitor's own label does appear in the answer text (`test_competitor_mention_is_detected_from_the_same_answer_text`). Not fixed in this branch; for accurate competitor detection today, point the competitor field at the specific product's own domain rather than a multi-product parent company's root domain.

October 7 (local dev, same model): after the branded-prompt fix, human-sounding-prompt fix, and weak-label fix, reran both audits as regression checks. linear.app vs. atlassian.com: score dropped from 94 to **53/100** on a mostly-unbranded, natural-sounding prompt set (5 unbranded + 2 branded shown separately, e.g. "anyone know a good tool for tracking ai agents", "issue tracker that actually feels fast", "what do startups use instead of jira") - a believable number, not the earlier inflated one; Atlassian still scored ~0 for the reason above (no unbranded answer in this sample said "Jira" either). cal.com vs. calendly.com: score was **29/100** (previously 0 in an earlier, different random sample - both are low, consistent, not identical since each run samples fresh prompts/answers); this time Calendly - same brand/product name, no domain-mismatch issue - was correctly detected as mentioned in 4 of 5 unbranded answers and scored well above cal.com on the chart, directly confirming competitor mentions are measured from the real answer text rather than assumed. Weak-bucket labels in both runs now only appear below 0.65 (e.g. "Weak pricing - 0.00", never a false "weak (0.96)"). All prompt-level explanations varied naturally instead of repeating one sentence.

October 7 (local dev): getting the branded-prompt fix to a passing live run took three rounds of tuning against `gemini-flash-lite-latest`'s actual behavior, not just the first instruction rewrite: (1) the model named the target/competitor in most prompts despite "mostly unbranded" instructions, traced to the tone few-shot examples themselves being majority-branded (3 of 4 cited examples named a brand) - rebalanced to mostly-unbranded examples with branded ones clearly marked as the occasional exception; (2) even after rebalancing, raw yield was inconsistent (3, 6, then 4 of ~10-12 raw prompts unbranded across manual test runs) - lowered generation temperature 0.7 to 0.4 and asked for more raw candidates (`PROMPT_GENERATION_ASK_MAX`) to give the branded/human-sounding filters more headroom; (3) `MIN_UNBRANDED_PROMPTS` (the hard floor that triggers a retry) was tuned down from `PROMPT_TARGET_MIN` (8) to 4, empirically, because 8 was never reliably achievable on this model across repeated real attempts. This number is specific to `gemini-flash-lite-latest`'s observed instruction-following and has not been re-validated against the production `gemini-2.5-flash` model, which may follow the brand-exclusion instruction better (or worse) - re-check `MIN_UNBRANDED_PROMPTS` once the 2.5-flash quota resets and is usable again.

October 7 (local dev): the originally-requested systematic 6-vertical sanity sweep (dev tool, consumer app, fintech, ecommerce, local small business, tiny startup) was interrupted before completion by this branded-prompt/weak-label/human-sounding-prompt fix work and not resumed in this session. Only linear.app/atlassian.com and cal.com/calendly.com have fresh post-fix data (above). The other pairs identified for that sweep (duolingo.com/babbel.com, mercury.com/brex.com, an ecommerce pair, a real small local business vs. booksy.com/zocdoc.com, and a near-zero-presence startup) were not run; that sweep should be redone now that the scoring and prompt-generation fixes are in, not with the pre-fix pipeline.

Links:
- Repository: https://github.com/anish-yen/visibility-next
- Merge: https://github.com/anish-yen/visibility-next/pull/4
- App: https://visibility-auditor.vercel.app
- Deployments: https://vercel.com/anis-projects-55f1b355/visibility-auditor/deployments

## Measurement contract

- Use varied, natural, unbranded buyer questions for headline visibility. Branded questions are a separate reputation/discoverability read, not evidence buyers independently find the brand. As of `fix/branded-prompt-filtering` (unmerged), this is actually enforced end to end: branded tagging checks the target and all competitors, branded prompts are capped and shown separately in the UI, and only the unbranded subset counts toward the score.
- Keep the same reviewed prompt set for before/after. Record engine/model, surface, timestamp, exact prompt, raw answer, cited URLs, mentions and errors.
- Distinguish model-knowledge answers, search-grounded API answers, consumer-web checks and simulated/injected-context tests. Do not blend them into a claimed ChatGPT ranking.
- Report mention rate, citation share, prominence and content readiness separately. A citation to a page is not automatically a recommendation of its brand.
- Failures and heuristic estimates must be visible. A fallback is not a successful real-engine measurement.
- Training memory and live retrieval are different mechanisms. Site changes cannot directly edit a model's training weights. Published changes may take time to become retrievable and may not improve answers.
- A model evaluating rewritten content that we injected is only a pre-check. It cannot prove independent discoverability or real-world lift.

## How to actually move GEO

SEO aims to improve search visibility across relevant keywords; GEO aims to raise mention rate across a battery of buyer prompts. Think "climb the search results" versus "raise the batting average," not optimize for one answer. Neither surface is fixed: search rankings and generated answers can vary by location, context, engine and time. An AI answer is generated rather than a permanent ranked list, but it need not change on every repeat.

Practical work split: SEO combines keyword research, descriptive titles/headers, useful pages, technical crawlability and performance, plus off-site backlinks. GEO adds emphasis on independent coverage and consistent descriptions, while still needing on-site answer-shaped, quotable content. "SEO mostly on-site, GEO mostly off-site" is a planning shorthand, not a hard boundary: backlinks are off-site, and FAQs/pricing/answer pages are on-site work for both. Neither a Reddit mention nor a backlink is universally more valuable; use the actual cited-source evidence for the category.

1. **Earn credible third-party mentions.** Make independent coverage a high-priority lever: relevant Reddit discussions, review sites, honest listicles and third-party comparison pages. Independent evidence can support recommendations better than self-praise. Earned-media research supports this direction, but which source moves a particular engine/category must be tested. Do not buy fake reviews or flood communities; posting and outreach still need approval.
2. **Make the site quotable.** Publish factual pages that directly answer buyer questions, with clear pricing/conditions, FAQs, use cases and honest comparisons. Remove vague claims and give the engine something specific it can retrieve and cite.
3. **Keep naming consistent.** Use the same product name and truthful category phrase across the site, documentation, profiles and approved third-party descriptions. Correct conflicting or outdated facts rather than repeating slogans.
4. **Measure before and after with the same prompts.** Capture a baseline, then repeat the frozen buyer-prompt battery 30 days later under comparable engine/model/surface settings. Primary metric: unbranded mention rate, defined as prompts whose answer names the brand divided by completed eligible prompts. Preserve counts, errors, citations and dates; keep branded results separate. Repeated samples help distinguish noise from a change. A higher rate is evidence to investigate, not proof one edit caused it or a guarantee of future placement.

This is the intended optimization playbook, not a claim the current app automates all four steps or a newly scheduled 30-day run. Sources: [earned-media and engine differences study](https://arxiv.org/html/2509.08919v1); [practical evidence, entity consistency and measurement guidance](https://auspia.ai/blog/trust-based-geo-ai-citations).

## Competitive context: Aeonza and the AEO market

Source: public pages read October 7, 2026. Competitor claims below are theirs, not verified.

- Aeonza (aeonza.com): two-person student agency founded 2026, "Answer Engine Optimization for Startups". Free audit as the hook, then a managed service to change content, authority signals and structure, with continuous re-testing. No public pricing. Claims 4 clients and results such as 11% to 64% AI visibility in 11 weeks; the case-study page did not load, so none of this is independently checked. It is the same audit-then-fix play Zeteum is aiming at, sold by hand.
- Monitoring tools (Otterly, Peec, AthenaHQ, Profound) track mentions across engines, roughly $29 to $500 a month for the self-serve tiers; Profound now appears to be enterprise-only. Prices come from vendor pages and third-party roundups that disagree, so re-check before quoting. Agencies run from low hundreds to $15,000+ a month. Monitoring is commoditized and mostly does not fix anything.

### SEO versus GEO, in plain terms

Anish's framing: rewriting site content is SEO; GEO is about presence on forums, reviews and third-party sources that live search pulls from. That is a good planning shorthand and matches the GEO playbook above. It is not a hard line: answer-shaped, quotable pages on the site still help AI answers, and third-party coverage usually does more. A rewrite alone is the weaker GEO lever.

### Differentiator: hypotheses, not established

1. Honest measurement: unbranded-only headline score with raw prompts, answers and citations visible. Holds only after the scoring bug is fixed and a real answer engine is verified; the default audit today uses Gemini model knowledge.
2. Audit, fix, re-measure in one loop. Code exists for the cycle path; end-to-end runtime is not verified.
3. Price and self-serve for very small brands, between a $29 tool and a hand-sold agency.
4. Gap: Zeteum does not yet do the third-party work (the bigger GEO lever); it only drafts posts for review. Aeonza's advantage today is people doing outreach and publishing case studies.

Do not claim any of these publicly until the evidence in the checklist below exists.

## Intended improvement workflow

1. Crawl target and competitors, respecting robots.txt and rate limits. Extract page types, positioning and verified facts.
2. Prompt generation: create and review realistic customer questions; freeze the measurement set.
3. Visibility evaluation: query the selected independent engine(s), collect actual answers/citations and label each measurement surface.
4. Diagnosis: connect weak prompts to missing/vague content, competitor evidence and third-party sources. Do not assume on-site copy is the only useful lever.
5. Rewrite: propose specific homepage, FAQ, comparison, metadata or structured-content changes using actual facts and the site's style. Mark unknown claims for confirmation; never invent testimonials or results.
6. Validation: check factual support, voice/style, answer relevance and proposed diffs. Simulated re-testing can reject weak drafts, but is not the success metric.
7. Human review and publication: Anish/customer approves actual changes. V1/V2 do not automatically edit a live customer website.
8. Independent re-test: after approved publication and retrieval, rerun the same raw buyer prompts without injecting rewritten copy. Compare dated evidence, report no change honestly, and repeat only within an approved iteration/cost limit.
9. Stop on plateau, iteration/cost cap, missing permission or unavailable evidence. The existing loop's numeric thresholds are implementation defaults, not universal product success criteria.

The initial PRD names Claude, LangGraph, Celery/Redis, 25-100 prompts and 2-3 samples. Appended implementation notes describe Gemini and 8-12 prompts. The present code uses a custom loop and in-process audit jobs. Treat the original architecture as a roadmap, not evidence those requirements are shipped. Verify current model availability, prompt counts and sampling before documenting them as guarantees.

## Perplexity Sonar upgrade and comparison protocol

Recommended once the MVP is stable and the spend is approved. The runner exists, but a reliable side-by-side report and successful live activation still need verification.

Why: obtain independent search-backed answers and citations rather than grading the business's copy after giving it to the evaluator. Citation evidence shows what the engine read. Sonar is its own API surface; it is not guaranteed identical to consumer Perplexity and does not measure consumer ChatGPT.

Protocol:

1. Freeze reviewed raw buyer prompts. The initial comparison proposal used the same 12 prompts, including a genuine small-business test. Keep branded prompts out of the headline set.
2. Send the raw prompt to Sonar with no injected target/competitor page content and no instruction to recommend the target.
3. Store model/engine, prompt, time, complete answer, citations, mentions and errors.
4. Run the existing controlled/model-knowledge evaluation on the exact same prompts. Present both lanes side by side, explicitly labeled, rather than hiding disagreement in an average.
5. Inspect disagreements and cited sources before making real-engine evidence primary. Keep simulation as a labeled fallback/pre-check, not claimed market visibility.
6. After approved site changes are published and retrievable, repeat the same independent query set and preserve before/after receipts.

Historical notes disagree on cost: the Google Doc said "$5/month"; September 25 conversation said roughly "$5 of credits, not a subscription." Neither is current pricing evidence. Check vendor pricing/limits and the intended total, then obtain approval before paid use. No payment is authorized by this plan.

## Aarav's scope and status

September 26 peer reports said Aarav would personally (a) review the forwarded "outbound with claude code" acquisition email and (b) test the auditor hands-on. There was no promised build implementation, PR, migration or deadline in those exchanges. His X post/avatar choice was feedback, not engineering delivery.

His side initially reviewed `visibilityaudit.ai` instead of the supplied Zeteum auditor. They withdrew attribution to Zeteum and said he would test `visibility-auditor.vercel.app`. The October 7 peer-history check found no subsequent correct-site findings. Do not use the other site's pricing/refund/checkout observations as Zeteum evidence. This is a communication-status record, not a claim about Git commit authorship.

## Historical results and superseded claims

- The old 24-to-65.5 "after" story used brand-naming prompts. It does not prove unbranded visibility improved in consumer answers.
- The later 67.9 result was on a different subject; it is not the next point in the same baseline series.
- Historical 0/8 logged-out tests are dated observations, not today's results.
- Earlier queued deployments, branch-only/main-untouched claims and email-wave plans are not current status. Main was merged and the frontend deployed; new cold email is parked.
- Original PRD simulated-only wording and a completed autonomous-loop claim both overstate one side of the current architecture. The ordinary audit and grounded cycle paths differ, and publication/re-test remains human-in-the-loop.

## Working rules

Prompt/scoring changes need Anish to see the prompts before push/deploy. Do not regress working behavior. Branch and test app changes before deployment. Public posts, prospect emails and DMs require review; no automatic outreach from this file. No account-recovery flows or key rotation. Secrets stay outside the repo and outside shared plan material.

Design direction: restrained typography and whitespace, one indigo accent, no generic AI-gradient styling. Rewrite copy must match the customer's real voice, not generic model prose.

## Keeping this plan current

This is an editable, version-controlled standing file, not an automatic synchronization service. Updating a Google Doc does not update this file; pushing this file does not guarantee an already-running Claude session has reread it.

At the start of code work, explicitly read `PLAN.md` alongside `CLAUDE.md` and relevant implementation notes. After each verified milestone, update this file in the same branch/PR: date and revision, exact change, evidence/tests, what remains unverified and the next step. Keep older evidence dated and mark superseded priorities rather than silently treating proposals as completed work. Review the Git diff for secrets before committing.

| Open item | Completion evidence required |
| --- | --- |
| Audit persistence | Approved migration, live write/read, survival across restart |
| Staging audit | Authenticated end-to-end run with prompts, answers, labeled source and failures |
| Scoring symmetry | Zero-mention and equal-mention regression tests — done on `fix/competitor-scoring-symmetry` (unmerged); still needs Anish's review/merge and a staging check |
| Small-brand validation | 3-5 dated real-brand audits with raw answers/citations |
| Style-guided rewrites | Site-derived style.md, factual draft/diff and owner review |
| Sonar comparison | Approved cost, successful independent queries, same-prompt side-by-side evidence |
| Public claims/outreach | Owner-reviewed final content and recipients/audience |

## Change log

- October 7, 2026: created sanitized standing plan from Anish's project notes, September 25 measurement discussion, September 26 Aarav exchange, October 5 deployment handoff and fresh main-code inspection. No credentials or keys tab copied. No app logic changed.

- October 7, 2026: added the GEO optimization playbook and 30-day same-prompt measurement checkpoint; clarified variability and evidence limits. Documentation only.

- October 7, 2026: added Aeonza/AEO market context, the SEO versus GEO framing and unverified differentiator hypotheses. Documentation only.

- October 7, 2026: fixed competitor scoring symmetry (priority 2) on branch `fix/competitor-scoring-symmetry`. Competitors now use the same per-prompt formula, weights and unbranded-prompt subset/denominator as the target; page completeness moved to a separate `content_readiness` field. Added 3 regression tests (`backend/tests/test_scoring.py`); all 19 backend tests pass. Verified locally against a real Gemini-backed audit. Also fixed a local-only `GEMINI_MODEL` sunset issue blocking all audits, and updated `.env.example`'s default. Not merged, not deployed; priority 1 (DB) remains blocked pending explicit DB go-ahead.

- October 7, 2026: fixed the branded-prompt scoring leak on branch `fix/branded-prompt-filtering` (stacked on the branch above). `names_brand` now checks target AND competitor names, branded prompts are capped at 2 and shown separately in the UI as reference-only, and generation retries if too few unbranded prompts survive. Also fixed generated prompts reading like SEO keyword stacks (rewrote the Gemini prompts, added a human-sounding-query heuristic filter), the "weak" bucket label firing on strong scores (now gated by `WEAK_BUCKET_THRESHOLD`), and repetitive per-prompt explanation text. 9 new tests across `test_prompt_quality.py` and `test_scoring.py`; all 28 backend tests pass; frontend typechecks clean. Live-reran linear.app/atlassian.com (94 -> 53/100, an honest drop) and cal.com/calendly.com (29/100, stayed low, Calendly's mentions now correctly detected and scored) as regressions - see dated evidence above for full numbers and for the separate Jira/Atlassian brand-label-mismatch limitation this surfaced (not fixed; a crawl/brand-identity issue, not a scoring bug). Also discovered and worked around a free-tier `gemini-2.5-flash` daily quota cap (20 requests/day) by temporarily using `gemini-flash-lite-latest` for this session's testing; `MIN_UNBRANDED_PROMPTS` was tuned against that lighter model's real behavior and should be re-checked against `gemini-2.5-flash` once its quota resets. The originally-requested 6-vertical sanity sweep was not completed - only 2 of 6 planned pairs got fresh post-fix data. Not merged, not deployed.
