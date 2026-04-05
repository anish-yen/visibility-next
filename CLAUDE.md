AI Search Visibility Auditor — PRD + HLD

> **Scope authority:** Use the attached PRD/HLD PDF as the source of truth for product scope and architecture. **V2** includes a multi-agent optimization loop (diagnose → rewrite → retest); it is **not** in the current MVP. This product delivers **directional simulated AI visibility** through a **controlled internal pipeline** — not live scraping of ChatGPT, Gemini, or Perplexity, and not autonomous editing of customer websites.

## Part 0 — Current implementation (living)

### Stack (as built)

| Layer | Choice |
| ----- | ------ |
| Frontend | Next.js 14, React, Tailwind, Supabase Auth (SSR) |
| Backend API | FastAPI |
| Auth validation | Supabase `GET /auth/v1/user` + `apikey` (no local JWT secret) |
| Audit storage | **In-memory** (`audit_store`) — no Postgres persistence for audits yet |
| Job execution | **asyncio** `create_task` on `POST /audits` — **Celery/Redis not wired** for audits |
| Crawler | **Real** `services/crawler.py`: sitemap/nav, `robots.txt`, 1 req/s, httpx + BeautifulSoup/lxml (**Playwright** in requirements for future JS sites) |
| LLM | **Google Gemini** via official **`google-genai`** SDK (`GEMINI_API_KEY`). Default model **`gemini-2.5-flash`** for prompts, visibility simulation, and briefs. **Anthropic/Claude is not the default.** |

### MVP pipeline (end-to-end)

1. **Crawl** primary domain and each competitor (`crawl_site`) — up to 20 pages each; failures logged, partial data kept.
2. **Prompt generation** — Gemini produces **15** JSON-tagged prompts (informational, comparative, transactional, trust).
3. **Visibility evaluation** — For each prompt, Gemini answers as a helpful assistant with optional crawl context; a second structured pass scores mention strength (**0 / 0.5 / 1.0**). Failures keep partial rows; overall score = average × 100.
4. **Recommendations** — Rule-based gaps from **actual** primary `page_type` set plus weak intent averages.
5. **Content brief** — `POST .../brief` calls Gemini (JSON brief) with fallback text if the API fails.

### Backend module map

- `app/main.py` — app, CORS, `/health`, `/test-crawl`, `/test-gemini`, `/api/me`
- `app/middleware.py` — Supabase token + anon key to Auth API
- `app/audit_store.py`, `app/audit_pipeline.py`
- `app/services/crawler.py`, `gemini_client.py`, `prompt_generator.py`, `visibility_evaluator.py`, `recommendations_builder.py`, `brief_generator.py`
- `app/routers/audits.py` — `/audits` API for the dashboard

### Environment variables (backend)

See repo `.env.example`. Required for the LLM pipeline: **`GEMINI_API_KEY`**. Supabase: **`SUPABASE_URL`**, **`SUPABASE_ANON_KEY`**, **`SUPABASE_SERVICE_KEY`** (service for admin client when used).

### Run locally

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

- **Gemini sanity:** open or curl `http://localhost:8000/test-gemini` (expects `GEMINI_API_KEY` in `backend/.env`).
- **Crawl smoke:** `http://localhost:8000/test-crawl?domain=stripe.com`

### Product messaging

Visibility scores are **simulated, directional estimates**. They must **not** be marketed as guaranteed rankings or live measurements inside third-party AI apps.

### Suggested next steps

- Migrate `AuditState` to Supabase/Postgres; add Celery when jobs exceed HTTP timeouts.
- Optional: Playwright path in crawler for heavy SPAs.
- **V2 only:** LangGraph / multi-agent orchestration — **do not add for MVP.**

---

Part 1: Product Requirements Document (PRD)

1. Product Name
   AI Search Visibility Auditor

2. Product Summary
   AI Search Visibility Auditor helps businesses understand how visible they are in AI-generated answers across tools like ChatGPT, Gemini, Perplexity, and similar answer engines. Users enter their website and competitors, and the product generates realistic customer prompts, evaluates whether the company appears in simulated AI answer-style outputs, identifies missing content opportunities, and produces actionable content briefs.
   Important: The product simulates AI-style answer evaluation using a controlled LLM pipeline. It does not directly query or access ChatGPT, Gemini, or Perplexity internals. Visibility scores are directional estimates, not guaranteed rankings.
   The goal is to make AI discoverability measurable and improveable for smaller businesses that do not have access to enterprise AI visibility tooling.

3. Problem Statement
   Traditional SEO tools are built for search engines that return links. AI answer engines increasingly return synthesized answers instead of blue links, which changes how businesses get discovered. Small and mid-sized companies do not know:
   whether they are appearing in AI-generated answers
   which competitor pages are being cited or reflected
   what content types are missing from their site
   what they should build to improve visibility
   Most current solutions are either enterprise-focused, expensive, or analytics-heavy without being execution-focused.

4. Vision
   Create a lightweight, affordable, self-serve platform that tells companies:
   how visible they are in AI-style search (via simulated evaluation)
   why they are or are not appearing
   what pages they should create next
   and gives them a usable first draft or content brief immediately

5. Goals
   Primary Goals
   Measure AI visibility for a company across a set of generated customer prompts
   Compare visibility against competitors
   Detect content gaps on the company site
   Recommend high-impact new content
   Generate content briefs automatically
   Secondary Goals
   Track visibility over time
   Categorize prompts by buyer intent
   Provide a simple scoring system for prioritization
   Make the system usable by non-technical founders and marketers

6. Non-Goals
   Building a full SEO suite
   Publishing content directly to CMS platforms in V1
   Guaranteeing rankings in third-party AI systems
   Reverse-engineering proprietary model internals (the product uses a controlled evaluation pipeline, not live AI engine access)
   Replacing human content strategy entirely
   Multi-agent autonomous content optimization (planned for V2 — see Section 20)

7. Target Users
   Primary Users
   Small business owners
   Startup founders
   Freelancers / agencies
   Growth marketers at early-stage companies
   Secondary Users
   Content strategists
   SEO consultants
   Product marketers
   AI-focused agencies

8. User Pain Points
   "I don't know if my brand shows up in ChatGPT or AI search."
   "I don't know what prompts customers would actually use."
   "I don't know which pages I'm missing."
   "I don't have time to do manual competitor analysis."
   "I want something more actionable than a dashboard."

9. User Stories
   As a business owner, I want to enter my website and competitors so I can compare AI visibility.
   As a marketer, I want the system to generate realistic customer prompts so I do not have to think of them manually.
   As a founder, I want to know which pages I am missing so I can prioritize content work.
   As a content writer, I want a content brief for a missing page so I can create it faster.
   As a user, I want a simple score that tells me where my best opportunities are.
   As a returning user, I want to rerun the audit and track changes over time.
   As a new user, I want to sign up and manage my projects in one place without needing technical setup.

10. Core Features
    10.1 Authentication and Account Management
    Users can sign up, log in, and manage their account. Each user has a plan tier that governs their usage limits. Password reset and session management are required for MVP.
    10.2 Website Audit Input
    User provides:
    company domain
    competitor domains (up to 5 in V1)
    optional company description
    optional target customer / industry
    optional region
    System validates that all submitted domains are reachable before starting the audit.
    10.3 Site Crawl and Content Extraction
    System crawls:
    homepage
    main navigation pages
    blog/resources
    FAQ pages
    comparison pages
    pricing pages
    reviews/testimonials pages
    product/service pages
    metadata and structured content
    The crawler respects robots.txt and enforces per-domain rate limits. Pages blocked by robots.txt are skipped and logged.
    10.4 Prompt Generation
    System generates 25–50 prompts per audit for free-tier users and up to 100 for Pro/Agency tiers. Prompt count scales with site page count within these bounds.
    Prompt categories:
    informational
    comparative
    transactional/commercial
    problem-solution
    local/vertical-specific
    trust/review-oriented
    Examples:
    "best payroll software for small law firms"
    "is X better than Y for ecommerce"
    "affordable bookkeeping software for startups"
    "best alternative to [competitor]"
    10.5 AI Visibility Testing
    For each generated prompt, the system sends the prompt to a controlled LLM evaluation pipeline (not to live AI engines) and evaluates:
    whether the target company is mentioned
    whether competitors are mentioned
    mention order/prominence
    whether the answer weakly, moderately, or strongly reflects site content
    whether product/category fit is correct
    To improve consistency, each prompt may be run 2–3 times and results averaged. Results are directional estimates, not measurements of real-world AI engine behavior.
    In the multi-agent loop (V2), the visibility evaluation agent does not rely on Google or any external search index to reflect updated content. Instead, updated page content is injected directly into the evaluation context each iteration. This ensures visibility scores reflect the latest rewritten content immediately, without waiting for any search engine to re-crawl the site.

10.6 Content Gap Detection
System determines whether the company lacks:
competitor comparison pages
clear FAQ pages
use-case pages
vertical landing pages
review/testimonial pages
pricing explanation pages
trust/credibility pages
glossary/definition pages for educational prompts
10.7 Opportunity Scoring
Each recommended page gets scored based on:
prompt demand (0.30)
competitor visibility advantage (0.25)
commercial intent relevance (0.20)
content absence severity (0.15)
estimated implementation effort (0.10)
These weights are initial hypotheses and will be tuned based on user feedback and outcome data.
10.8 Content Brief Generator
For a recommended page, system outputs:
page title
search/prompt intent
target audience
key questions to answer
competitor angles
suggested sections/H2s
proof/trust elements to include
internal links to add
CTA suggestions
10.9 Dashboard
Dashboard shows:
overall visibility score
prompt coverage
competitor comparison
missing page opportunities
recommended content priority list
history of previous audits

11. Error States
    The system must handle and surface the following failure cases to users:
    Domain unreachable: Notify user before audit starts; do not silently fail.
    Crawl blocked (robots.txt or bot protection): Log which pages were skipped and show the user a partial result with a coverage warning.
    Evaluation failure mid-audit: Save completed results and surface partial scores with a notice.
    Brief generation failure: Show a retry option; do not block the rest of the dashboard.

12. Differentiators
    Built for smaller businesses, not just enterprises
    Focuses on actionability, not only analytics
    Generates content briefs instead of just identifying problems
    Uses prompt-based AI visibility simulation, not just classic SEO signals
    Prioritizes page-type recommendations by business impact

13. Functional Requirements
    Auth
    User can sign up with email/password
    User can log in, log out, and reset password
    User session is tied to plan tier
    Audit Setup
    User can submit one primary domain and up to 5 competitors in V1
    User can edit company description before audit runs
    System validates reachable domains
    Crawl
    System extracts page URLs, titles, headings, and main content
    System categorizes pages by type
    System stores crawl snapshots
    System respects robots.txt and rate limits
    Prompt Generation
    Free tier: 25–50 prompts per audit
    Pro/Agency tier: up to 100 prompts per audit
    User can regenerate or edit prompts
    Prompts are tagged by intent
    Visibility Analysis
    System evaluates each prompt via a controlled LLM pipeline
    System stores visibility results per prompt
    System computes aggregate visibility metrics
    Each prompt run 2–3 times for consistency; scores are averaged
    Recommendation Engine
    System identifies missing content categories
    System ranks recommendations by priority score
    System links recommendations to specific prompt gaps
    Brief Generation
    User can click a recommendation and generate a content brief
    Brief export (markdown/PDF) planned for later versions
    History
    User can view previous audits
    User can compare audit snapshots over time

14. Non-Functional Requirements
    Audit should complete within 2–10 minutes for small sites
    System should support at least 100 prompts per run in V1
    Crawl must respect robots.txt and enforce rate limits (max 1 request/sec per domain)
    Visibility results for the same prompt run twice should agree on brand mention presence ≥80% of the time
    Sensitive customer inputs must be stored securely
    System should support future multi-tenant SaaS deployment

15. Success Metrics
    Metric
    MVP Target
    Audit completion rate
    ≥75% of started audits complete successfully
    Prompt review rate
    ≥60% of users view the prompt table
    Recommendation click rate
    ≥40% of users click at least one recommendation
    Content brief generation rate
    ≥25% of users generate at least one brief
    Repeat audit rate
    ≥20% of users run a second audit within 30 days
    Free-to-paid conversion
    ≥5% within 60 days of signup

16. MVP Scope
    In Scope
    Auth (signup, login, password reset)
    Domain input and validation
    Basic crawl with robots.txt compliance
    Prompt generation (25–100 based on tier)
    Visibility scoring via controlled LLM pipeline
    Competitor comparison
    Content gap detection
    Ranked recommendations
    Content brief generation
    Dashboard with audit results and history
    MVP will be tested on real publicly accessible company domains. Domain input accepts any valid public URL.
    Out of Scope
    CMS publishing
    Team collaboration
    Advanced reporting exports
    Multi-model benchmarking
    Real-time monitoring
    Browser extension
    Multi-agent optimization loop (V2)

17. Monetization
    Feature
    Free
    Pro
    Agency
    Domains
    1
    Unlimited
    Multiple clients
    Competitors
    2
    5
    5 per client
    Prompts per audit
    25
    100
    100
    Audits per week
    1
    Unlimited
    Unlimited
    Content briefs
    2
    Unlimited
    Unlimited
    Audit history
    No
    Yes
    Yes
    Exports
    No
    Yes
    Yes
    White-label reports
    No
    No
    Yes
    Team accounts
    No
    No
    Yes

Pricing to be determined after MVP user validation.

18. Risks
    LLM outputs can vary and reduce consistency (mitigated by multi-sample averaging)
    AI visibility is partly dependent on external systems you do not control
    Crawling some websites can be difficult due to JS rendering or bot protection
    Recommendation quality may be weak without strong page classification
    Users may expect guaranteed ranking improvements (mitigated by clear product messaging)
    Crawling competitor sites carries legal/ethical risk (mitigated by robots.txt compliance and rate limiting)

19. Assumptions
    Businesses care about AI answer engine visibility
    Prompt simulation can approximate real customer questions reasonably well
    Missing content types correlate with lower visibility
    Content briefs are a useful enough first step for smaller teams
    For MVP testing and demo purposes, the system will be validated against real existing company websites (e.g. paypal.com, stripe.com) by scraping their public content. Any user will be able to submit any domain in the production version.

20. V2 Roadmap: Multi-Agent Optimization Workflow
    A future version of the system will include a multi-agent optimization loop. One agent continuously evaluates whether the company appears in AI-style search responses for important customer prompts. A diagnosis agent identifies likely causes of weak visibility (unclear positioning, missing comparisons, weak FAQs). A content generation agent proposes revised page copy or new page drafts. A re-test agent evaluates whether the revised content improves simulated visibility scores. An orchestrator manages the loop, stopping after a configurable number of iterations or when improvement plateaus.
    In V2, the system will not directly edit live websites. It will output suggested rewrites and content diffs for user review and approval.
    V2 User Story: As a business owner, I want the system not only to tell me what is missing, but to iteratively refine my content suggestions and check whether those changes would improve my AI visibility score.

21. Future Roadmap (Post-V2)
    Recurring monitoring
    Team collaboration
    Direct CMS publishing
    Citation/source tracking
    Page-level rewrite suggestions
    Integration with Search Console / analytics / CRM
    Answer-engine-specific benchmarking

Part 2: High-Level Design (HLD)

1. System Overview
   The system takes a company domain and competitors, crawls their websites, extracts structured content, generates likely customer prompts, evaluates AI-style visibility via a controlled LLM pipeline, identifies content gaps, scores opportunities, and generates content briefs. Results are exposed in a web dashboard. Audit processing is fully asynchronous.

2. Major Components
   Frontend Web App
   API / Orchestration Backend
   Auth Service
   Crawler + Content Extraction Service
   Prompt Generation Engine
   Visibility Evaluation Engine
   Content Gap Analyzer
   Recommendation + Scoring Engine
   Content Brief Generator
   Database / Storage Layer
   Job Queue / Async Processing Layer

3. Architecture Style
   Modular service-oriented architecture with async job processing:
   Frontend sends audit request
   Backend validates input and creates audit job
   Worker processes crawl → generation → evaluation steps sequentially
   Results saved in database after each step
   Frontend polls for completion status
   For MVP, all backend services live in a single codebase with separate modules. Components can be extracted to independent services in V2.

4. Tech Stack
   Layer
   Choice
   Rationale
   Frontend
   Next.js + React + Tailwind CSS
   Fast iteration, good DX, easy deployment
   Charts
   Recharts
   Lightweight, React-native
   Backend
   FastAPI (Python)
   Async support, fast iteration, Python ecosystem for AI/crawling
   Database
   PostgreSQL (via Supabase)
   Managed, easy setup, supports pgvector if needed later
   Crawling
   Playwright + BeautifulSoup/lxml
   Handles JS-rendered sites
   AI/LLM
   Google Gemini API (google-genai SDK; e.g. gemini-2.5-flash)
   Prompt generation, simulated visibility eval, content brief generation
   Embeddings
   Sentence-transformers or hosted API
   Optional for semantic similarity
   Job Queue
   Celery + Redis (planned; MVP audits use asyncio background tasks + in-memory store)
   Reliable async job processing when persistence and scale require it
   Auth
   Supabase Auth
   Built-in, covers signup/login/password reset
   Frontend Hosting
   Vercel
   Zero-config Next.js deployment
   Backend Hosting
   Render or Railway
   Simple Python/FastAPI deployment

5. Logical Flow
   Step 1: User Registers / Logs In
   └── Supabase Auth handles session

Step 2: User Creates Audit
└── Input: domain, competitors, optional metadata
└── Backend: validate domains → create Audit record → enqueue job

Step 3: Crawl and Extract Content
└── Crawler fetches sitemap → traverses core pages
└── Respects robots.txt; skips blocked pages
└── Extracts title, meta, headings, body text
└── Classifies page type (rule-based in MVP)
└── Stores normalized page records

Step 4: Prompt Generation
└── Uses site content + industry + competitor names + templates
└── Generates 25–100 prompts tagged by intent
└── User can regenerate or edit

Step 5: Visibility Evaluation
└── Each prompt → controlled LLM pipeline
└── Detects company + competitor mentions
└── Scores mention prominence and relevance
└── Runs 2–3 samples per prompt; averages scores

Step 6: Content Gap Analysis
└── Compares missing page types against prompt underperformance
└── Identifies competitor content advantages

Step 7: Opportunity Scoring
└── Weighted formula ranks recommendations

Step 8: Content Brief Generation
└── Triggered per recommendation
└── Structured JSON output → user-facing text

Step 9: Display Results
└── Dashboard: summary scores, charts, prompt table, opportunities, briefs

6. Core Data Model
   User
   id, email, plan_type, created_at
   Project / Workspace
   id, user_id, company_name, primary_domain, industry, created_at
   Audit
   id, project_id, status, created_at, completed_at, config_json
   Competitor
   id, audit_id, domain, display_name
   Page
   id, audit_id, domain, url, page_type, title, meta_description, headings_json, content_text, word_count, crawl_status, robots_blocked (boolean)
   Prompt
   id, audit_id, prompt_text, intent_type, topic_cluster, generated_by
   VisibilityResult
   id, audit_id, prompt_id, target_domain, mentioned_boolean, mention_strength, mention_order, competitor_mentions_json, answer_summary, score, confidence, sample_count
   ContentGap
   id, audit_id, gap_type, description, supporting_prompts_json, competitor_evidence_json, severity_score
   Recommendation
   id, audit_id, recommendation_type, title, rationale, priority_score, effort_score, confidence_score
   ContentBrief
   id, recommendation_id, title, audience, intent, outline_json, key_points_json, cta_json, generated_text, created_at

7. Key Modules
   7.1 Auth Service
   Supabase Auth handles signup, login, password reset, and session tokens. The backend validates JWT tokens on every request. Project and audit records are scoped to the authenticated user.
   7.2 Crawl Service
   Fetch sitemap if available; otherwise traverse nav links
   Respect robots.txt before fetching any page
   Rate limit: max 1 request/second per domain
   Use Playwright for JS-rendered pages; fall back to requests + BeautifulSoup for static HTML
   Extract: title, meta description, H1–H3, body text, internal links
   Page type classification (MVP: rule-based on URL patterns and headings; V2: LLM-based)
   Page types: homepage, pricing, FAQ, comparison, blog, review/testimonial, use-case, docs/help, contact/about
   7.3 Prompt Generation Engine
   Template-based generation seeded with site content, industry, competitor names
   LLM refinement to make prompts natural and varied
   Output: prompt list with intent tags and topic clusters
   Free tier: 25–50 prompts; Pro/Agency: up to 100
   7.4 Visibility Evaluation Engine
   The engine does not query live AI engines. It:
   Sends each prompt through a controlled Gemini (or equivalent) pipeline as "answer this as a customer-facing AI assistant would"
   Parses the response to detect company and competitor mentions
   Scores by: mention presence, order, relevance, strength
   Runs 2–3 samples per prompt; returns averaged score and confidence
   visibility_score = weighted_sum(mentioned, order, relevance, confidence)
   Results are directional estimates. Consistency target: ≥80% agreement on brand mention presence across repeated runs.
   7.5 Content Gap Analyzer
   Cross-references:
   which prompts the company underperforms on
   which page types are missing from the crawl
   which competitor pages exist that the company lacks
   Example rule: company underperforms on comparison prompts + no comparison pages found → recommend "Build competitor comparison pages"
   7.6 Recommendation Engine
   Weighted priority formula:
   Signal
   Weight
   Prompt demand
   0.30
   Competitive disadvantage
   0.25
   Commercial intent
   0.20
   Missing content severity
   0.15
   Confidence
   0.10

Weights are initial estimates to be validated and tuned post-launch.
7.7 Content Brief Generator
Uses recommendation context + prompt cluster + site context + competitor findings to generate a structured brief:
Page objective
Target prompt cluster
Audience pain points
Suggested H2 headings
Proof points and trust signals
FAQs
CTA
Suggested internal links
Stored as structured JSON internally; rendered as readable text in the dashboard.

8. API Design
   POST /auth/signup Create user account
   POST /auth/login Authenticate user
   POST /auth/password-reset Trigger password reset

POST /audits Create a new audit
GET /audits/{id} Audit status and summary
GET /audits/{id}/prompts Generated prompts
GET /audits/{id}/results Visibility results
GET /audits/{id}/recommendations Ranked opportunities

POST /recommendations/{id}/brief Generate content brief
GET /briefs/{id} Return content brief

GET /projects List user projects
POST /projects Create project

9. Background Job Design
   Audit processing is fully async. Job stages:
   queued — validate input
   crawling — crawl target domain and competitors
   generating_prompts — generate and tag prompts
   evaluating — run visibility evaluation
   analyzing — detect gaps and score recommendations
   generating_briefs — generate top briefs (optional, triggered on demand)
   completed — mark audit done
   failed — log error and surface partial results if available
   Each stage saves results before moving to the next. If a stage fails, partial data is preserved and the user is notified.

10. Caching Strategy
    Cache the following to reduce cost and latency:
    Crawl results per domain (TTL: 24 hours)
    Competitor crawl snapshots (TTL: 24 hours)
    Prompt generation results for the same domain+industry combination (TTL: 12 hours)
    Repeated content brief requests for the same recommendation

11. Security / Privacy
    Sanitize all crawled content before storage
    Do not execute arbitrary scripts from crawled pages
    Rate limit all API endpoints
    Enforce user auth and project ownership on every request
    Encrypt API keys and secrets at rest
    Clearly disclose in the product UI that AI visibility scores are simulated estimates, not live measurements

12. Observability
    Track:
    Crawl success/failure rates and pages blocked by robots.txt
    Average audit duration per stage
    LLM token usage per audit
    Visibility score consistency (variance across repeated prompt runs)
    Content brief generation success rate
    Recommendation click-through rate
    Tools: application logs, audit event logs, Sentry for error monitoring, simple metrics dashboard.

13. Scalability (MVP → V2)
    MVP: Single backend process with Celery workers; low concurrency; small sites only.
    Scaling path:
    Separate crawl workers from LLM workers
    Queue-based architecture already in place via Celery/Redis
    Cache repeated competitor analyses
    Rate-limit outbound crawl and LLM requests independently

14. Tradeoffs
    Fast MVP: One backend codebase with separate modules. Less architecturally "clean" but faster to ship and validate.
    Accuracy: Prompt-based visibility is an approximation via a controlled LLM pipeline, not a measurement of real AI engine behavior. Good enough for directional recommendations; messaging must be clear.
    Cost: Generating many prompts and running multi-sample evaluations increases LLM cost. Free-tier prompt limits and cheaper models for intermediate steps help control this.

15. MVP Build Order
    Phase
    Scope
    Phase 1
    Auth, frontend input form, audit creation, basic crawler, page extraction, page-type tagging
    Phase 2
    Prompt generation, visibility evaluation, simple scoring
    Phase 3
    Recommendation engine, dashboard views, brief generation
    Phase 4
    Audit history, exports, recurring runs

16. Architecture Summary
    A user signs up, submits a domain and competitors, and the backend crawls their sites (respecting robots.txt), generates likely customer prompts, evaluates visibility via a controlled LLM pipeline, detects missing content, ranks the best opportunities by weighted score, and generates content briefs — all surfaced in a dashboard with audit history.

Part 3: 2-Week MVP Roadmap
Assumptions
Solo developer or very small team (1–2 people)
Tech stack confirmed: Next.js frontend, FastAPI backend, Supabase (auth + DB); MVP audits use asyncio + in-memory store; Gemini API (google-genai) for LLM steps; Celery + Redis when jobs are persisted and scaled.
Goal: working end-to-end demo by end of Week 2

Week 1 — Foundation and Data Pipeline
Day
Focus
Deliverables
Day 1
Project setup
Repo, environments, Supabase project, Next.js scaffold, FastAPI scaffold wired together. Auth (signup/login/password reset) working end to end.
Day 2
Database schema
All tables created: User, Project, Audit, Competitor, Page, Prompt, VisibilityResult, ContentGap, Recommendation, ContentBrief. Migrations run cleanly.
Day 3
Crawl service
Crawler fetches pages, respects robots.txt, rate-limits to 1 req/sec, classifies page types by URL pattern, stores Page records. Tested on 3–5 real domains.
Day 4
Audit job pipeline
Celery + Redis setup. Audit job created on POST /audits. Stages: validate → crawl → mark complete. Status polling endpoint working. Frontend shows job status.
Day 5
Prompt generation
Prompt engine generates prompts from crawled content + Gemini (JSON). Prompts tagged by intent. Stored (DB when migrated; in-memory for current MVP). Frontend displays prompt list.

Week 1 Exit Criteria: User can sign up, submit a domain, see it crawled, and see generated prompts.

Week 2 — Evaluation, Recommendations, and Dashboard
Day
Focus
Deliverables
Day 6
Visibility evaluation
Gemini-based simulated evaluation runs per prompt. Detects company + competitor mentions, scores visibility. Results stored. (Multi-sample averaging 2–3 runs is a future tuning step.)
Day 7
Content gap detection
Analyzer cross-references missing page types against low-visibility prompts. ContentGap records generated.
Day 8
Recommendation engine + scoring
Weighted scoring formula applied. Ranked Recommendation records generated. GET /audits/{id}/recommendations returns sorted list.
Day 9
Content brief generation
POST /recommendations/{id}/brief calls Gemini to generate structured brief. Brief stored and retrievable. Frontend shows brief in a readable format.
Day 10
Dashboard + polish
Visibility score summary, competitor comparison, prompt table, opportunity list, brief viewer all wired into the dashboard. Error states handled (blocked crawl, failed eval). Basic audit history view.

Week 2 Exit Criteria: Full end-to-end flow working — sign up, audit, view scores, see recommendations, generate a brief.

What Comes After Week 2
User testing with 3–5 real small businesses
Tune prompt generation quality
Tune recommendation scoring weights based on feedback
Add export (markdown brief download)
Add recurring audit scheduling
Begin V2 multi-agent loop design
