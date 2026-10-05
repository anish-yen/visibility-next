from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from typing import Any

from app import audit_store
from app.services.crawler import crawl_site, distill_site_context
from app.services.gemini_client import GeminiClient, GeminiError

PROMPT_TARGET_MIN = 8
PROMPT_TARGET_MAX = 12
MAX_PROMPT_CHARS = 120


def _allow_heuristic_fallback() -> bool:
    """Dev-only escape hatch: heuristic prompts/scores are never user-facing by default."""
    return os.environ.get("ALLOW_HEURISTIC_FALLBACK", "").strip().lower() in {"1", "true", "yes"}


async def _gemini_with_backoff(operation: Any, *, attempts: int, label: str) -> Any:
    delays = (12.0, 35.0, 80.0)
    last_exc: GeminiError | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except GeminiError as exc:
            last_exc = exc
            print(f"{label} failed (attempt {attempt + 1}/{attempts}): {exc}", flush=True)
            if attempt < attempts - 1:
                base = delays[min(attempt, len(delays) - 1)]
                await asyncio.sleep(base + random.uniform(0.0, base * 0.4))
    if last_exc is not None:
        raise last_exc
    raise GeminiError(f"{label} failed")
PROMPT_BUCKETS = (
    "informational",
    "comparative",
    "pricing",
    "trust",
    "implementation",
    "use_case",
)
ALLOWED_PROMPT_INTENTS = {
    "informational",
    "comparative",
    "transactional",
    "trust",
    "pricing",
    "implementation",
    "use_case",
}
NAV_TERMS = {
    "products",
    "solutions",
    "developers",
    "resources",
    "pricing",
    "docs",
    "documentation",
    "support",
    "company",
    "about",
    "contact",
    "login",
    "sign in",
    "request demo",
    "contact sales",
}

BANNED_MARKETING_PROMPT_PATTERNS = (
    "next big thing",
    "be the next big thing",
    "powering",
    "grow your revenue",
    "new business opportunities",
)


def _brand_label(domain: str) -> str:
    part = domain.split(".")[0]
    return part.replace("-", " ").title()


def _page_type_present(site: dict[str, Any], page_type: str) -> bool:
    return bool(site.get("page_type_counts", {}).get(page_type, 0))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _summarize_prompt_weaknesses(prompts: list[dict[str, Any]]) -> dict[str, float]:
    summary: dict[str, list[float]] = {}
    for prompt in prompts:
        intent = prompt.get("intent") or "other"
        summary.setdefault(intent, []).append(float(prompt.get("score", 0.0)))
    return {
        intent: round(sum(scores) / len(scores), 3)
        for intent, scores in summary.items()
        if scores
    }


def _bucket_average_scores(prompts: list[dict[str, Any]]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for prompt in prompts:
        bucket = _coverage_bucket(prompt)
        buckets.setdefault(bucket, []).append(float(prompt.get("score", 0.0)))
    return {
        bucket: round(sum(values) / len(values), 3)
        for bucket, values in buckets.items()
        if values
    }


def _bucket_counts(prompts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        bucket: sum(1 for prompt in prompts if _coverage_bucket(prompt) == bucket)
        for bucket in PROMPT_BUCKETS
    }


def _normalize_text(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return " ".join(lowered.split())


def _normalize_intent_label(intent: str, text: str) -> str:
    cleaned = intent.strip().lower()
    if cleaned in ALLOWED_PROMPT_INTENTS:
        return cleaned
    bucket = _coverage_bucket({"text": text})
    if bucket == "pricing":
        return "pricing"
    if bucket == "implementation":
        return "implementation"
    if bucket == "use_case":
        return "use_case"
    return "comparative" if bucket == "comparative" else "trust" if bucket == "trust" else "informational"


def _has_repeated_phrase(text: str) -> bool:
    words = _normalize_text(text).split()
    if len(words) < 6:
        return False
    three_grams = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    return len(three_grams) != len(set(three_grams))


def _prompt_looks_polluted(text: str, blocked_fragments: list[str]) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    if len(text) > MAX_PROMPT_CHARS:
        return True
    if text.count("|") > 0 or text.count("...") > 0 or text.count(":") > 1:
        return True
    if any(term in normalized for term in NAV_TERMS) and len(normalized.split()) <= 12:
        return True
    if _has_repeated_phrase(text):
        return True
    if any(pattern in normalized for pattern in BANNED_MARKETING_PROMPT_PATTERNS):
        return True
    if "for businesses" in normalized and len(normalized.split()) <= 6:
        return True
    if sum(text.count(ch) for ch in "|/<>") >= 2:
        return True
    for fragment in blocked_fragments:
        if fragment and fragment in normalized and len(fragment.split()) >= 8:
            return True
    return False


def _dedupe_prompts(prompts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: list[set[str]] = []
    for prompt in prompts:
        token_set = set(_normalize_text(prompt["text"]).split())
        if not token_set:
            continue
        duplicate = False
        for existing in seen:
            overlap = len(token_set & existing)
            union = max(len(token_set | existing), 1)
            if overlap / union >= 0.72:
                duplicate = True
                break
        if duplicate:
            continue
        seen.append(token_set)
        unique.append(prompt)
    return unique


def _compress_prompt(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    compact = compact.strip(" -|,;:")
    compact = compact.replace(" ?", "?")
    compact = compact.replace(" .", ".")
    if "." in compact:
        compact = compact.split(".")[0].strip()
    if len(compact) > MAX_PROMPT_CHARS:
        compact = compact[:MAX_PROMPT_CHARS].rsplit(" ", 1)[0].strip()
    return compact


def _extract_category_phrase(distilled: dict[str, Any], brand: str) -> str:
    candidates = [
        distilled.get("normalized_category", ""),
        distilled.get("product_category", ""),
        distilled.get("category", ""),
        distilled.get("summary", ""),
        *distilled.get("use_cases", []),
        *distilled.get("keywords", []),
    ]
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized:
            continue
        normalized = re.sub(rf"\b{re.escape(brand)}\b", "", normalized, flags=re.IGNORECASE).strip(" ,.-")
        normalized = re.sub(r"\bto grow your revenue\b", "", normalized, flags=re.IGNORECASE).strip(" ,.-")
        normalized = re.sub(r"\bfor businesses\b", "", normalized, flags=re.IGNORECASE).strip(" ,.-")
        normalized = normalized[:60].strip()
        if 2 <= len(normalized.split()) <= 6:
            return normalized.lower()
    return "software"


def _extract_target_customer(distilled: dict[str, Any]) -> str:
    normalized_category = str(distilled.get("normalized_category", "")).lower()
    if normalized_category == "knowledge management":
        return "knowledge workers"
    if normalized_category == "team collaboration software":
        return "collaborative teams"
    if normalized_category == "project management software":
        return "project teams"
    if normalized_category == "productivity tool":
        return "knowledge workers"
    for candidate in distilled.get("normalized_use_cases", []):
        lowered = candidate.lower()
        if "marketplace" in lowered:
            return "marketplace teams"
        if "ecommerce" in lowered:
            return "ecommerce teams"
        if "subscription" in lowered:
            return "SaaS teams"
    for candidate in distilled.get("audiences", []):
        lowered = candidate.lower()
        if "startup" in lowered:
            return "startups"
        if "small business" in lowered or "small team" in lowered:
            return "small businesses"
        if "enterprise" in lowered:
            return "enterprise teams"
        if "developer" in lowered:
            return "developer-led teams"
        if "marketplace" in lowered:
            return "marketplace teams"
        if "saas" in lowered:
            return "SaaS teams"
        if "ecommerce" in lowered:
            return "ecommerce teams"
    return "growing teams"


def _extract_use_case_phrases(distilled: dict[str, Any]) -> list[str]:
    normalized = [item.strip().lower() for item in distilled.get("normalized_use_cases", []) if item.strip()]
    if normalized:
        return normalized[:2]

    candidates = distilled.get("use_cases", []) + distilled.get("keywords", [])
    phrases: list[str] = []
    for candidate in candidates:
        text = candidate.strip()
        lowered = text.lower()
        if not text:
            continue
        if any(term in lowered for term in ("subscription", "billing", "marketplace", "checkout", "payments", "fraud", "invoic", "onboard", "api", "ecommerce")):
            cleaned = re.sub(r"\bfor\b.*", "", lowered).strip(" ,.-")
            cleaned = cleaned[:50].strip()
            if 1 <= len(cleaned.split()) <= 6:
                phrases.append(cleaned)
    unique: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        if phrase not in seen:
            seen.add(phrase)
            unique.append(phrase)
    return unique[:2]


def _extract_trust_theme(distilled: dict[str, Any]) -> str:
    if distilled.get("normalized_trust"):
        return str(distilled["normalized_trust"])
    for candidate in distilled.get("trust_signals", []):
        lowered = candidate.lower()
        if "compliance" in lowered or "security" in lowered:
            return "security and compliance"
        if "customer" in lowered or "testimonial" in lowered or "case stud" in lowered:
            return "customer proof"
        if "reliable" in lowered or "trusted" in lowered:
            return "reliability"
    return "customer proof"


def _category_query_label(category: str) -> str:
    lowered = category.strip().lower()
    if lowered == "payment processing":
        return "payment processor"
    if lowered == "online payments":
        return "payment platform"
    if lowered == "billing and subscriptions":
        return "billing platform"
    if lowered == "marketplace payments":
        return "payment platform"
    if lowered == "financial infrastructure for businesses":
        return "payments platform"
    if lowered == "knowledge management":
        return "knowledge management software"
    if lowered == "team collaboration software":
        return "collaboration software"
    if lowered == "project management software":
        return "project management software"
    if lowered == "productivity tool":
        return "productivity software"
    return lowered


def _fallback_prompt_specs(
    *,
    brand: str,
    competitor: str,
    category: str,
    customer: str,
    use_cases: list[str],
    trust_theme: str,
) -> list[tuple[str, str]]:
    primary_use_case = use_cases[0] if use_cases else category
    secondary_use_case = use_cases[1] if len(use_cases) > 1 else customer
    query_category = _category_query_label(category)
    return [
        ("informational", f"best {query_category}"),
        ("informational", f"best {query_category} for {primary_use_case}"),
        ("comparative", f"{brand} vs {competitor} for {primary_use_case}"),
        ("comparative", f"{competitor} alternatives for {query_category}"),
        ("transactional", f"{query_category} with transparent pricing"),
        ("transactional", f"easiest {query_category} to set up"),
        ("trust", f"is {brand} good for {primary_use_case}"),
        ("trust", f"{query_category} with strong {trust_theme}"),
        ("transactional", f"{brand} pricing"),
        ("informational", f"best platform for {primary_use_case}"),
        ("transactional", f"{query_category} for {customer}"),
        ("informational", f"{competitor} competitors for {query_category}"),
        ("transactional", f"{query_category} with easy onboarding"),
        ("use_case", f"{query_category} for {primary_use_case}"),
        ("use_case", f"{brand} for {secondary_use_case}"),
    ]


def _build_template_prompt_rows(
    *,
    brand: str,
    competitor: str,
    category: str,
    customer: str,
    use_cases: list[str],
    trust_theme: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for intent, text in _fallback_prompt_specs(
        brand=brand,
        competitor=competitor,
        category=category,
        customer=customer,
        use_cases=use_cases,
        trust_theme=trust_theme,
    ):
        rows.append({"id": str(uuid.uuid4()), "text": text, "intent": intent})
    return rows


def _coverage_bucket(prompt: dict[str, Any]) -> str:
    text = _normalize_text(prompt["text"])
    if any(term in text for term in ("vs", "alternative", "alternatives", "competitor", "compared")):
        return "comparative"
    if any(term in text for term in ("price", "pricing", "cost", "plans")):
        return "pricing"
    if any(term in text for term in ("trusted", "review", "proof", "case study", "reliable", "secure", "compliance")):
        return "trust"
    if any(term in text for term in ("implement", "integration", "integrations", "onboard", "setup", "api")):
        return "implementation"
    if any(term in text for term in ("marketplace", "subscriptions", "billing", "checkout", "ecommerce", "saas")):
        return "use_case"
    return "informational"


def _intent_alignment_score(prompt: dict[str, Any], target_site: dict[str, Any]) -> float:
    bucket = _coverage_bucket(prompt)
    page_support = {
        "comparative": 1.0 if _page_type_present(target_site, "comparison") else 0.35,
        "pricing": 1.0 if _page_type_present(target_site, "pricing") else 0.35,
        "trust": 1.0 if _page_type_present(target_site, "reviews") else 0.4,
        "implementation": 1.0 if _page_type_present(target_site, "docs") else 0.4,
        "use_case": 1.0 if any(_page_type_present(target_site, page_type) for page_type in ("blog", "docs", "general")) else 0.55,
        "informational": 0.85 if _page_type_present(target_site, "homepage") else 0.5,
    }
    return page_support.get(bucket, 0.6)


def _compute_prompt_score(
    *,
    target_role: str,
    competitor_role: str,
    fit_score: float,
    prompt: dict[str, Any],
    target_site: dict[str, Any],
    competitor_mentions: list[str],
) -> tuple[float, dict[str, float]]:
    mention_prominence = {"absent": 0.0, "supporting": 0.65, "central": 1.0}.get(target_role, 0.0)
    competitor_penalty = {"none": 0.0, "supporting": 0.12, "strong": 0.24}.get(competitor_role, 0.0)
    competitor_penalty += min(0.08, max(0, len(competitor_mentions) - 1) * 0.04)
    intent_alignment = _intent_alignment_score(prompt, target_site)
    answer_fit = fit_score

    raw_score = (
        (mention_prominence * 0.45)
        + (answer_fit * 0.3)
        + (intent_alignment * 0.2)
        - competitor_penalty
    )

    if target_role == "central":
        raw_score += 0.08
    elif target_role == "absent":
        raw_score = 0.0

    bounded = round(max(0.0, min(1.0, raw_score)), 2)
    components = {
        "mention_prominence": round(mention_prominence, 2),
        "fit_to_prompt": round(answer_fit, 2),
        "intent_alignment": round(intent_alignment, 2),
        "competitor_penalty": round(competitor_penalty, 2),
    }
    return bounded, components


def _ensure_prompt_coverage(
    prompts: list[dict[str, Any]],
    *,
    state: audit_store.AuditState,
    target_distilled: dict[str, Any],
    competitor_distilled: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    brand = _brand_label(state.primary_domain)
    competitor = (
        competitor_distilled[0].get("label")
        if competitor_distilled
        else (state.competitor_domains[0] if state.competitor_domains else "competitors")
    )
    category = state.industry or _extract_category_phrase(target_distilled, brand)
    customer = _extract_target_customer(target_distilled)
    use_cases = _extract_use_case_phrases(target_distilled)
    trust_theme = _extract_trust_theme(target_distilled)

    existing = prompts[:]
    covered = {_coverage_bucket(prompt) for prompt in existing}
    fallback_candidates = _build_template_prompt_rows(
        brand=brand,
        competitor=competitor,
        category=category,
        customer=customer,
        use_cases=use_cases,
        trust_theme=trust_theme,
    )
    sanitized_candidates = _sanitize_generated_prompts(
        fallback_candidates,
        target_distilled=target_distilled,
        competitor_distilled=competitor_distilled,
        check_blocked_fragments=False,
    )

    for bucket in PROMPT_BUCKETS:
        if bucket in covered:
            continue
        candidate = next((item for item in sanitized_candidates if _coverage_bucket(item) == bucket), None)
        if candidate:
            existing.append(candidate)
            covered.add(bucket)

    for candidate in sanitized_candidates:
        if len(existing) >= PROMPT_TARGET_MIN:
            break
        if _normalize_text(candidate["text"]) not in {_normalize_text(item["text"]) for item in existing}:
            existing.append(candidate)

    existing = _dedupe_prompts(existing)
    if len(existing) < PROMPT_TARGET_MIN:
        direct_fill = _dedupe_prompts(sanitized_candidates + existing)
        existing = direct_fill[: max(PROMPT_TARGET_MIN, len(direct_fill))]
    return existing[:PROMPT_TARGET_MAX]


def _sanitize_generated_prompts(
    raw_prompts: list[dict[str, Any]],
    *,
    target_distilled: dict[str, Any],
    competitor_distilled: list[dict[str, Any]],
    check_blocked_fragments: bool = True,
) -> list[dict[str, Any]]:
    blocked_fragments = [
        _normalize_text(target_distilled.get("summary", "")),
        _normalize_text(target_distilled.get("category", "")),
    ]
    blocked_fragments.extend(_normalize_text(item) for item in target_distilled.get("keywords", []))
    for site in competitor_distilled:
        blocked_fragments.append(_normalize_text(site.get("summary", "")))
        blocked_fragments.append(_normalize_text(site.get("category", "")))

    cleaned: list[dict[str, Any]] = []
    for item in raw_prompts:
        if not isinstance(item, dict):
            continue
        text = _compress_prompt(str(item.get("text", "")).strip())
        intent = _normalize_intent_label(str(item.get("intent", "informational")), text)
        if not text:
            continue
        if _prompt_looks_polluted(text, blocked_fragments if check_blocked_fragments else []):
            continue
        if len(text.split()) < 4:
            continue
        cleaned.append({"id": str(uuid.uuid4()), "text": text, "intent": intent})

    return _dedupe_prompts(cleaned)[:PROMPT_TARGET_MAX]


def _fallback_prompts(
    state: audit_store.AuditState,
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    brand = _brand_label(state.primary_domain)
    target_distilled = distill_site_context(target_site)
    competitor_distilled = [distill_site_context(site) for site in competitor_sites]
    competitor = (
        competitor_distilled[0].get("label")
        if competitor_distilled
        else (state.competitor_domains[0] if state.competitor_domains else "competitors")
    )
    category = state.industry or _extract_category_phrase(target_distilled, brand)
    customer = _extract_target_customer(target_distilled)
    use_cases = _extract_use_case_phrases(target_distilled)
    trust_theme = _extract_trust_theme(target_distilled)
    prompts = _build_template_prompt_rows(
        brand=brand,
        competitor=competitor,
        category=category,
        customer=customer,
        use_cases=use_cases,
        trust_theme=trust_theme,
    )
    cleaned = _sanitize_generated_prompts(
        prompts,
        target_distilled=target_distilled,
        competitor_distilled=competitor_distilled,
        check_blocked_fragments=False,
    )
    return _ensure_prompt_coverage(
        cleaned,
        state=state,
        target_distilled=target_distilled,
        competitor_distilled=competitor_distilled,
    )


async def _refine_target_category_with_gemini(target_site: dict[str, Any]) -> None:
    client = GeminiClient()
    distilled = distill_site_context(target_site)
    evidence = {
        "domain": target_site.get("domain"),
        "label": distilled.get("label"),
        "summary": distilled.get("summary"),
        "keywords": (distilled.get("keywords") or [])[:12],
        "page_highlights": distilled.get("page_highlights") or [],
        "heuristic_category_guess": distilled.get("category") or "",
    }
    system_instruction = (
        "You identify what a company sells from its website text. Return strict JSON only."
    )
    user_prompt = f"""
Based on the website evidence below, identify the company precisely.

Return JSON with this exact shape:
{{
  "category": "string",
  "use_cases": ["string"],
  "target_customer": "string"
}}

Rules:
- "category": what the company SELLS, 2-6 lowercase words, e.g. "ai visibility software" or "payments infrastructure". A category, not a tagline, not a use case, not the company name.
- "use_cases": up to 4 short buyer use-case phrases (2-5 words each) grounded in the evidence.
- "target_customer": who buys it, 2-5 words.
- Correct the heuristic guess when it is wrong.
- No explanations or text outside the JSON object.

Website evidence:
{json.dumps(evidence, ensure_ascii=True)}
"""
    data = await client.generate_json(
        system_instruction=system_instruction,
        user_prompt=user_prompt,
        temperature=0.2,
    )
    category = str(data.get("category", "")).strip().lower()
    if not (3 <= len(category) <= 80) or len(category.split()) > 8 or "|" in category:
        raise GeminiError("Gemini category refinement returned an unusable category")
    use_cases_raw = data.get("use_cases") or []
    if not isinstance(use_cases_raw, list):
        use_cases_raw = []
    use_cases = [
        str(item).strip().lower()
        for item in use_cases_raw
        if 3 <= len(str(item).strip()) <= 60
    ][:4]
    target_customer = str(data.get("target_customer", "")).strip().lower()[:80]

    target_site["category_override"] = category
    if use_cases:
        target_site["use_cases_override"] = use_cases
    if target_customer:
        target_site["customer_override"] = target_customer
    target_site["category_source"] = "gemini"
    print(
        f"Gemini category refinement: {category} (customer: {target_customer or 'unknown'})",
        flush=True,
    )


async def _generate_prompts_with_gemini(
    state: audit_store.AuditState,
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    client = GeminiClient()
    target_distilled = distill_site_context(target_site)
    competitor_distilled = [distill_site_context(site) for site in competitor_sites if site.get("domain")]
    competitor_labels = [site.get("label") or _brand_label(site.get("domain", "")) for site in competitor_sites]
    competitor_context = json.dumps(
        [
            {
                "label": site.get("label"),
                "summary": site.get("summary"),
                "keywords": site.get("keywords", []),
                "page_types": site.get("page_type_counts", {}),
            }
            for site in competitor_distilled
            if site.get("summary")
        ],
        ensure_ascii=True,
    )

    system_instruction = (
        "You write buyer questions the way a real person actually types into an AI assistant, "
        "logged out with no context: casual, direct, lowercase is fine, sometimes a bare search "
        "phrase, sometimes a full question. The buyer is hunting for a tool or brand to solve "
        "their use case but usually has NOT named a brand yet. Return JSON only. "
        "Real examples of this register, actually typed by a human into ChatGPT: "
        "\"service that audits whether chatgpt mentions your product\", "
        "\"alternatives to Profound for ai visibility\", "
        "\"how do i get my startup cited by ai chatbots\", "
        "\"What is the best AI visibility tool for a B2B startup?\", "
        "\"Profound vs Peec: which one tracks ChatGPT mentions better?\", "
        "\"How do I get ChatGPT to recommend my SaaS product to buyers?\". "
        "Vary length across the set: mix short 4-8 word fragments with full questions, and never "
        "use the same opening shape twice. "
        "Bad prompts are telegraphic template fragments like \"Acme for project planning\" or "
        "\"best platform for payments apis\" - never write those. "
        "Ground every prompt in what the company actually sells from the crawl evidence. "
        "Do not paste source text, titles, menus, taglines, or paragraphs from the website. "
        "Do not include explanations or any text outside the JSON object. "
        "Return strict JSON only."
    )
    user_prompt = f"""
Generate {PROMPT_TARGET_MIN} to {PROMPT_TARGET_MAX} natural prompts grounded in the distilled company context below.

Requirements:
- Each prompt must read like a real buyer query someone would type into an AI assistant or search box.
- Each prompt must be one sentence only.
- Keep each prompt under {MAX_PROMPT_CHARS} characters.
- Use the real brand/category language from the crawl, but do not copy long source text.
- Cover these areas across the full set: informational, comparative, pricing, trust/reviews, implementation/onboarding, and use-case queries.
- Mostly unbranded: at most 2 prompts in the set may name the company or a competitor. Every other prompt must name NO brand at all - a brand-agnostic buyer question whose best answer would naturally mention them.
- Vary length and shape: mix short fragments and full questions; no repeated openings like "how do i" more than twice.
- BANNED shapes: "<Brand> for <use case>", "best platform for <use case>", "is <Brand> good for <use case>", or any prompt that reads like filled template slots.
- Avoid placeholders like "your market", "your product", or generic filler.
- Avoid raw nav/menu text such as "Products Solutions Developers Resources Pricing".
- Avoid pasting titles with separators like "|" or long taglines.
- Include competitor names in at least one comparison question when a competitor is provided.
- Prefer concrete buyer wording like "vs", "pricing", "alternatives", "how do I", "which tool", "worth it".
- No explanations, bullet points, or notes.

Return JSON with this exact shape:
{{
  "prompts": [
    {{
      "text": "string",
      "intent": "informational|comparative|transactional|trust"
    }}
  ]
}}

Primary site distilled context:
{json.dumps(target_distilled, ensure_ascii=True)}

Competitor labels: {competitor_labels}

Competitor distilled context:
{competitor_context or "No competitor pages were crawled successfully."}
"""
    data = await client.generate_json(
        system_instruction=system_instruction,
        user_prompt=user_prompt,
        temperature=0.7,
    )
    prompts = data.get("prompts")
    if not isinstance(prompts, list):
        raise GeminiError("Prompt generation response did not contain a prompts list")

    cleaned = _sanitize_generated_prompts(
        prompts,
        target_distilled=target_distilled,
        competitor_distilled=competitor_distilled,
    )
    if len(cleaned) < PROMPT_TARGET_MIN:
        raise GeminiError("Prompt generation returned too few usable prompts")
    variants = _brand_variants(
        target_site.get("label") or _brand_label(target_site.get("domain", "")),
        target_site.get("domain", ""),
        target_site,
    )
    for prompt in cleaned[:PROMPT_TARGET_MAX]:
        lowered = str(prompt.get("text", "")).lower()
        prompt["names_brand"] = any(_find_brand(lowered, v) >= 0 for v in variants)
    return cleaned[:PROMPT_TARGET_MAX]


def _brand_variants(label: str, domain: str, site: dict[str, Any]) -> list[str]:
    variants: set[str] = set()
    for raw in (label, domain):
        cleaned = str(raw or "").strip().lower()
        if not cleaned:
            continue
        variants.add(cleaned)
        root = cleaned.split(".")[0].replace("-", " ").strip()
        if len(root) >= 3 and " " not in root:
            variants.add(root)
    pages = site.get("pages", []) if isinstance(site, dict) else []
    homepage = next(
        (page for page in pages if page.get("page_type") == "homepage"),
        pages[0] if pages else {},
    ) or {}
    title = str(homepage.get("title", "")).strip().lower()
    if title:
        brand_part = title
        for sep in ("|", " - ", " : ", ":"):
            if sep in brand_part:
                brand_part = brand_part.split(sep)[0].strip()
                break
        if 3 <= len(brand_part) <= 25 and " " not in brand_part:
            variants.add(brand_part)
        first_word = brand_part.split()[0] if brand_part.split() else ""
        if len(first_word) >= 4:
            variants.add(first_word)
    return sorted(variant for variant in variants if len(variant) >= 3)


def _find_brand(lowered: str, variant: str) -> int:
    start = 0
    while True:
        idx = lowered.find(variant, start)
        if idx < 0:
            return -1
        before = lowered[idx - 1] if idx > 0 else " "
        after_idx = idx + len(variant)
        after = lowered[after_idx] if after_idx < len(lowered) else " "
        if not before.isalpha() and not after.isalpha():
            return idx
        start = idx + 1


async def _evaluate_prompt(
    *,
    prompt: dict[str, Any],
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> dict[str, Any]:
    client = GeminiClient()
    target_label = target_site.get("label") or _brand_label(target_site.get("domain", ""))
    competitor_labels = [
        site.get("label") or _brand_label(site.get("domain", ""))
        for site in competitor_sites
        if site.get("domain")
    ]

    answer_system = (
        "You are a helpful AI assistant answering a prospective buyer's question. "
        "Answer from your own knowledge, the way a public AI assistant would answer any user. "
        "Recommend specific brands or products when they are relevant. "
        "Be concise: two to five sentences."
    )
    answer = await client.generate_text(
        system_instruction=answer_system,
        user_prompt=prompt["text"],
        temperature=0.4,
    )

    lowered = answer.lower()
    target_variants = _brand_variants(target_label, target_site.get("domain", ""), target_site)
    target_pos = -1
    for variant in target_variants:
        pos = _find_brand(lowered, variant)
        if pos >= 0 and (target_pos < 0 or pos < target_pos):
            target_pos = pos

    competitor_positions: dict[str, int] = {}
    for site in competitor_sites:
        if not site.get("domain"):
            continue
        label = site.get("label") or _brand_label(site.get("domain", ""))
        for variant in _brand_variants(label, site.get("domain", ""), site):
            pos = _find_brand(lowered, variant)
            if pos >= 0 and (label not in competitor_positions or pos < competitor_positions[label]):
                competitor_positions[label] = pos

    competitor_mentions = sorted(competitor_positions, key=competitor_positions.get)

    if target_pos < 0:
        target_role = "absent"
    elif not competitor_positions or target_pos <= min(competitor_positions.values()):
        target_role = "central"
    else:
        target_role = "supporting"

    if not competitor_mentions:
        competitor_role = "none"
    elif target_role == "absent":
        competitor_role = "strong"
    elif len(competitor_mentions) >= 2 and target_role != "central":
        competitor_role = "strong"
    else:
        competitor_role = "supporting"

    fit_score = {"central": 0.85, "supporting": 0.55, "absent": 0.0}[target_role]

    mention_strength, score_components = _compute_prompt_score(
        target_role=target_role,
        competitor_role=competitor_role,
        fit_score=fit_score,
        prompt=prompt,
        target_site=target_site,
        competitor_mentions=competitor_mentions,
    )

    prominence_label = {
        "central": "mentioned first among tracked brands",
        "supporting": "mentioned after other brands",
        "absent": "not mentioned",
    }[target_role]
    explanation = f"Real Gemini answer: target {prominence_label}"
    if competitor_mentions:
        explanation += f"; competitors mentioned: {', '.join(competitor_mentions)}"
    explanation += "."

    return {
        "id": prompt["id"],
        "text": prompt["text"],
        "intent": prompt.get("intent"),
        "mentioned": target_role != "absent",
        "names_brand": bool(prompt.get("names_brand")),
        "score": round(mention_strength, 2),
        "explanation": explanation,
        "competitor_mentions": competitor_mentions,
        "score_components": {
            **score_components,
            "target_role": target_role,
            "competitor_role": competitor_role,
        },
        "answer": answer,
        "evaluation_source": "gemini_live",
    }


def _fallback_evaluation(
    prompt: dict[str, Any],
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> dict[str, Any]:
    text = prompt["text"].lower()
    target_label = (target_site.get("label") or "").lower()
    target_distilled = distill_site_context(target_site)
    target_summary = " ".join(
        [
            target_distilled.get("summary", ""),
            target_distilled.get("category", ""),
            " ".join(target_distilled.get("keywords", [])),
        ]
    ).lower()
    competitor_mentions: list[str] = []
    strength = 0.0
    intent = prompt.get("intent") or "informational"
    fit_bonus = 0.0

    for site in competitor_sites:
        label = str(site.get("label", "")).strip()
        if label and label.lower() in text:
            competitor_mentions.append(label)

    if target_label and target_label in text:
        strength = 0.84
    elif any(keyword in text for keyword in ("pricing", "price", "cost", "plans")):
        strength = 0.72 if _page_type_present(target_site, "pricing") else 0.24
    elif any(keyword in text for keyword in ("compare", "vs", "versus", "alternative")):
        strength = 0.68 if _page_type_present(target_site, "comparison") else 0.22
    elif any(keyword in text for keyword in ("review", "testimonial", "trusted", "proof")):
        strength = 0.63 if _page_type_present(target_site, "reviews") else 0.2
    elif any(word in target_summary for word in text.split()[:5]):
        strength = 0.46

    if intent == "transactional" and _page_type_present(target_site, "pricing"):
        fit_bonus += 0.05
    if intent == "trust" and _page_type_present(target_site, "reviews"):
        fit_bonus += 0.04
    if intent == "comparative" and _page_type_present(target_site, "comparison"):
        fit_bonus += 0.05

    strength += fit_bonus
    strength = round(max(0.0, min(1.0, strength)), 2)

    mentioned = strength > 0
    target_role = "central" if strength >= 0.78 else "supporting" if strength >= 0.4 else "absent"
    competitor_role = "strong" if len(competitor_mentions) >= 2 else "supporting" if competitor_mentions else "none"
    computed_score, score_components = _compute_prompt_score(
        target_role=target_role,
        competitor_role=competitor_role,
        fit_score=strength,
        prompt=prompt,
        target_site=target_site,
        competitor_mentions=competitor_mentions,
    )

    return {
        "id": prompt["id"],
        "text": prompt["text"],
        "intent": prompt.get("intent"),
        "mentioned": computed_score > 0,
        "names_brand": bool(prompt.get("names_brand")),
        "score": computed_score,
        "explanation": "Fallback heuristic based on page-type coverage, prompt intent, and competitor presence.",
        "competitor_mentions": competitor_mentions,
        "score_components": {
            **score_components,
            "target_role": target_role,
            "competitor_role": competitor_role,
        },
        "answer": "",
        "evaluation_source": "heuristic",
    }


def _build_competitor_scores(
    state: audit_store.AuditState,
    prompts: list[dict[str, Any]],
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]], float, dict[str, Any]]:
    unbranded = [p for p in prompts if not p.get("names_brand")]
    branded = [p for p in prompts if p.get("names_brand")]
    headline = unbranded if unbranded else prompts
    score_scope = "unbranded" if unbranded else "all_prompts_fallback"
    total_prompts = max(len(headline), 1)
    target_total = sum(float(prompt.get("score", 0.0)) for prompt in headline)
    target_mention_rate = round(
        _safe_ratio(sum(1 for prompt in headline if prompt.get("mentioned")), total_prompts),
        2,
    )
    bucket_scores = _bucket_average_scores(headline)
    score_components = {
        "score_scope": score_scope,
        "average_prompt_score": round(_safe_ratio(target_total, total_prompts), 3),
        "target_mention_rate": target_mention_rate,
        "bucket_scores": bucket_scores,
        "bucket_counts": _bucket_counts(headline),
        "unbranded": {
            "prompt_count": len(unbranded),
            "mention_rate": round(_safe_ratio(sum(1 for p in unbranded if p.get("mentioned")), max(len(unbranded), 1)), 2),
            "average_prompt_score": round(_safe_ratio(sum(float(p.get("score", 0.0)) for p in unbranded), max(len(unbranded), 1)), 3),
        },
        "branded": {
            "prompt_count": len(branded),
            "mention_rate": round(_safe_ratio(sum(1 for p in branded if p.get("mentioned")), max(len(branded), 1)), 2),
            "average_prompt_score": round(_safe_ratio(sum(float(p.get("score", 0.0)) for p in branded), max(len(branded), 1)), 3),
        },
        "all_prompts": {
            "prompt_count": len(prompts),
            "mention_rate": round(_safe_ratio(sum(1 for p in prompts if p.get("mentioned")), max(len(prompts), 1)), 2),
            "average_prompt_score": round(_safe_ratio(sum(float(p.get("score", 0.0)) for p in prompts), max(len(prompts), 1)), 3),
        },
    }

    bucket_weights = {
        "comparative": 0.22,
        "pricing": 0.18,
        "trust": 0.18,
        "implementation": 0.14,
        "use_case": 0.14,
        "informational": 0.14,
    }
    present_weight = sum(w for b, w in bucket_weights.items() if b in bucket_scores)
    weighted_bucket_score = (
        sum(bucket_scores[b] * w for b, w in bucket_weights.items() if b in bucket_scores) / present_weight
        if present_weight
        else 0.0
    )
    score_components["weighted_bucket_score"] = round(weighted_bucket_score, 3)
    overall_score = round(
        (
            (score_components["average_prompt_score"] * 0.55)
            + (target_mention_rate * 0.2)
            + (weighted_bucket_score * 0.25)
        )
        * 100,
        1,
    )

    scores = [
        {
            "domain": state.primary_domain,
            "score": overall_score,
            "label": "You",
        }
    ]

    label_to_domain = {
        (site.get("label") or _brand_label(site.get("domain", ""))): site.get("domain")
        for site in competitor_sites
        if site.get("domain")
    }
    for site in competitor_sites:
        label = site.get("label") or _brand_label(site.get("domain", ""))
        mention_count = sum(
            1
            for prompt in prompts
            if label in prompt.get("competitor_mentions", [])
        )
        page_strength = 0.45 if site.get("pages") else 0.0
        comparison_bonus = 0.15 if _page_type_present(site, "comparison") else 0.0
        review_bonus = 0.1 if _page_type_present(site, "reviews") else 0.0
        competitor_score = round(
            min(
                100.0,
                ((_safe_ratio(mention_count, total_prompts) * 0.65) + page_strength + comparison_bonus + review_bonus) * 100,
            ),
            1,
        )
        scores.append(
            {
                "domain": label_to_domain.get(label) or site.get("domain"),
                "score": competitor_score,
                "label": label,
            }
        )

    return overall_score, scores, target_mention_rate, score_components


def _brief_type_for_recommendation(recommendation: dict[str, Any]) -> str:
    title = recommendation.get("title", "").lower()
    if "faq" in title or "objection" in title:
        return "faq"
    if "comparison" in title:
        return "comparison"
    if "homepage" in title or "positioning" in title:
        return "homepage"
    if "pricing" in title:
        return "pricing"
    if "trust" in title or "proof" in title or "evidence" in title:
        return "trust"
    if "implementation" in title or "onboarding" in title or "docs" in title:
        return "implementation"
    return "use_case"


def _format_rationale(
    *,
    bucket_name: str,
    bucket_score: float,
    page_coverage_note: str,
    evidence_note: str | None = None,
) -> str:
    rationale = f"{bucket_name.title()} prompts are weak ({bucket_score:.2f})"
    if page_coverage_note:
        rationale += f"; {page_coverage_note}"
    if evidence_note:
        rationale += f"; {evidence_note}"
    return rationale + "."


def _fallback_brief_body(
    *,
    audit: audit_store.AuditState,
    recommendation: dict[str, Any],
    weakest_prompts: list[dict[str, Any]],
) -> str:
    brief_type = _brief_type_for_recommendation(recommendation)
    evidence = recommendation.get("recommendation_evidence", {})
    weak_lines = "\n".join(f"- {prompt['text']}" for prompt in weakest_prompts)
    evidence_lines = "\n".join(
        f"- {key}: {value}"
        for key, value in [
            ("Weak buckets", evidence.get("weak_prompt_buckets")),
            ("Page coverage", evidence.get("page_coverage")),
            ("Example prompts", evidence.get("example_prompts")),
        ]
        if value
    )
    if brief_type == "comparison":
        return (
            f"## Objective\n\n"
            f"Create a comparison page that helps buyers evaluate `{audit.primary_domain}` against named alternatives and understand best-fit scenarios.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Comparison prompts underperformed.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Who this comparison is for and what they are evaluating.\n"
            f"2. Direct comparison table: pricing model, implementation effort, support, and fit.\n"
            f"3. Where `{audit.primary_domain}` wins, where competitors win, and honest tradeoffs.\n"
            f"4. Customer proof, migration confidence, and trust signals.\n"
            f"5. CTA for demo, trial, or sales-assisted evaluation.\n"
        )
    if brief_type == "faq":
        return (
            f"## Objective\n\n"
            f"Publish an FAQ/objections page that answers the buying questions blocking visibility for `{audit.primary_domain}`.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Informational/objection prompts underperformed.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Top objections with short, direct answers.\n"
            f"2. Buying questions on pricing, migration, contracts, and integrations.\n"
            f"3. Trust questions on security, support, and reliability.\n"
            f"4. Internal links to deeper docs, pricing, and implementation pages.\n"
            f"5. CTA for self-serve next step or sales contact.\n"
        )
    if brief_type == "homepage":
        return (
            f"## Objective\n\n"
            f"Sharpen homepage positioning so buyers and AI-generated answers can identify the offer, buyer, and use case faster.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Homepage/informational positioning is weak.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Hero with a clear category label and ideal buyer.\n"
            f"2. Primary use cases immediately below the hero.\n"
            f"3. Proof, customer logos, or quantified outcomes near the top of page.\n"
            f"4. Fast paths to pricing, docs, and comparison content.\n"
            f"5. CTA aligned to evaluation stage rather than generic brand copy.\n"
        )
    if brief_type == "implementation":
        return (
            f"## Objective\n\n"
            f"Create onboarding and implementation content that reduces perceived setup risk for `{audit.primary_domain}`.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Implementation/onboarding prompts underperformed.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Setup overview, required inputs, and time-to-value timeline.\n"
            f"2. Integration paths, APIs, and common implementation patterns.\n"
            f"3. Migration plan or onboarding checklist.\n"
            f"4. Support model, docs, and escalation paths.\n"
            f"5. CTA for implementation consultation or docs start point.\n"
        )
    if brief_type == "pricing":
        return (
            f"## Objective\n\n"
            f"Improve pricing clarity so evaluators can understand fit, packaging, and tradeoffs without friction.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Pricing prompts underperformed.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Pricing overview with packaging model and cost drivers.\n"
            f"2. Example scenarios by buyer type, volume, or use case.\n"
            f"3. FAQ on fees, limits, discounts, and contract questions.\n"
            f"4. Links to implementation, support, and comparison content.\n"
            f"5. CTA to calculate fit or talk to sales.\n"
        )
    if brief_type == "trust":
        return (
            f"## Objective\n\n"
            f"Create trust and proof content that makes `{audit.primary_domain}` feel safer and more credible during evaluation.\n\n"
            f"## Why now\n\n"
            f"{recommendation['rationale']}\n\n"
            f"## Evidence\n\n{evidence_lines or '- Trust/proof prompts underperformed.'}\n\n"
            f"## Prompt evidence\n\n{weak_lines}\n\n"
            f"## Recommended sections\n\n"
            f"1. Customer proof and quantified outcomes.\n"
            f"2. Security, compliance, and reliability assurances.\n"
            f"3. Testimonials, case studies, or review excerpts.\n"
            f"4. Why buyers trust the platform for critical workflows.\n"
            f"5. CTA to review proof, talk to sales, or start a trial.\n"
        )
    return (
        f"## Objective\n\n"
        f"Build a focused page around one of the weak buyer-intent themes surfaced in the audit.\n\n"
        f"## Why now\n\n"
        f"{recommendation['rationale']}\n\n"
        f"## Evidence\n\n{evidence_lines or '- Use-case prompts underperformed.'}\n\n"
        f"## Prompt evidence\n\n{weak_lines}\n\n"
        f"## Recommended sections\n\n"
        f"1. Clear target use case and who it is for.\n"
        f"2. Workflow detail, implementation notes, and fit criteria.\n"
        f"3. Proof and differentiation for that use case.\n"
        f"4. FAQ/objections specific to the workflow.\n"
        f"5. CTA for the next evaluation step.\n"
    )


def _generate_recommendations(
    state: audit_store.AuditState,
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    weaknesses = _summarize_prompt_weaknesses(prompts)
    weak_prompt_buckets = _bucket_average_scores(prompts)
    target_distilled = distill_site_context(target_site)
    competitor_labels = [site.get("label") or _brand_label(site.get("domain", "")) for site in competitor_sites if site.get("domain")]
    primary_competitor = competitor_labels[0] if competitor_labels else "key competitors"
    weak_prompts = sorted(prompts, key=lambda prompt: prompt.get("score", 0.0))[:4]
    weak_prompt_text = "; ".join(prompt["text"] for prompt in weak_prompts)
    comparison_weak = any(_coverage_bucket(prompt) == "comparative" and prompt.get("score", 0.0) < 0.65 for prompt in prompts)
    pricing_weak = any(_coverage_bucket(prompt) == "pricing" and prompt.get("score", 0.0) < 0.65 for prompt in prompts)
    trust_weak = any(_coverage_bucket(prompt) == "trust" and prompt.get("score", 0.0) < 0.65 for prompt in prompts)
    implementation_weak = any(_coverage_bucket(prompt) == "implementation" and prompt.get("score", 0.0) < 0.65 for prompt in prompts)
    recommendations: list[dict[str, Any]] = []

    def add_recommendation(title: str, rationale: str, priority_score: float, recommendation_evidence: dict[str, Any]) -> None:
        recommendations.append(
            {
                "id": f"rec-{state.id[:8]}-{len(recommendations)}",
                "title": title,
                "rationale": rationale,
                "priority_score": round(priority_score, 2),
                "recommendation_evidence": recommendation_evidence,
                "brief": None,
            }
        )

    if not _page_type_present(target_site, "faq"):
        add_recommendation(
            "Build a buyer FAQ and objections page",
            _format_rationale(
                bucket_name="informational",
                bucket_score=weak_prompt_buckets.get("informational", 0.0),
                page_coverage_note="no FAQ/help page was found",
                evidence_note="buyers likely are not getting direct answers to evaluation questions",
            ),
            0.93 if weaknesses.get("informational", 1.0) < 0.55 else 0.79,
            {
                "weak_prompt_buckets": {"informational": weak_prompt_buckets.get("informational", 0.0)},
                "page_coverage": {"faq": False},
                "example_prompts": [p["text"] for p in weak_prompts if _coverage_bucket(p) == "informational"][:3],
            },
        )
    if not _page_type_present(target_site, "comparison") or comparison_weak:
        competitor_has_comparison = any(_page_type_present(site, "comparison") for site in competitor_sites)
        add_recommendation(
            f"Strengthen comparison coverage against {primary_competitor}",
            _format_rationale(
                bucket_name="comparative",
                bucket_score=weak_prompt_buckets.get("comparative", 0.0),
                page_coverage_note="target comparison coverage is missing or thin",
                evidence_note=("competitors already show comparison coverage" if competitor_has_comparison else f"buyers are comparing against {primary_competitor}"),
            ),
            0.96 if weaknesses.get("comparative", 1.0) < 0.6 else 0.84,
            {
                "weak_prompt_buckets": {"comparative": weak_prompt_buckets.get("comparative", 0.0)},
                "page_coverage": {
                    "target_comparison_page": _page_type_present(target_site, "comparison"),
                    "competitor_comparison_pages": competitor_has_comparison,
                },
                "competitor": primary_competitor,
                "example_prompts": [p["text"] for p in weak_prompts if _coverage_bucket(p) == "comparative"][:3],
            },
        )
    if not _page_type_present(target_site, "pricing") or pricing_weak:
        add_recommendation(
            "Clarify pricing and packaging for evaluators",
            _format_rationale(
                bucket_name="pricing",
                bucket_score=weak_prompt_buckets.get("pricing", 0.0),
                page_coverage_note=("pricing content is missing" if not _page_type_present(target_site, "pricing") else "pricing content is not answering evaluator questions clearly enough"),
                evidence_note="buyers may not be getting enough clarity on plans, costs, or fit",
            ),
            0.9 if weaknesses.get("transactional", 1.0) < 0.62 else 0.78,
            {
                "weak_prompt_buckets": {"pricing": weak_prompt_buckets.get("pricing", 0.0)},
                "page_coverage": {"pricing": _page_type_present(target_site, "pricing")},
                "example_prompts": [p["text"] for p in weak_prompts if _coverage_bucket(p) == "pricing"][:3],
            },
        )
    if not _page_type_present(target_site, "reviews") or trust_weak:
        add_recommendation(
            "Expand trust content with proof and customer evidence",
            _format_rationale(
                bucket_name="trust",
                bucket_score=weak_prompt_buckets.get("trust", 0.0),
                page_coverage_note=("review/proof content is missing" if not _page_type_present(target_site, "reviews") else "existing proof content is not strong enough"),
                evidence_note="case studies, testimonials, or compliance detail are likely underrepresented",
            ),
            0.88 if weaknesses.get("trust", 1.0) < 0.62 else 0.75,
            {
                "weak_prompt_buckets": {"trust": weak_prompt_buckets.get("trust", 0.0)},
                "page_coverage": {"reviews": _page_type_present(target_site, "reviews")},
                "example_prompts": [p["text"] for p in weak_prompts if _coverage_bucket(p) == "trust"][:3],
            },
        )
    if implementation_weak and not _page_type_present(target_site, "docs"):
        add_recommendation(
            "Add onboarding and implementation content",
            _format_rationale(
                bucket_name="implementation",
                bucket_score=weak_prompt_buckets.get("implementation", 0.0),
                page_coverage_note="docs/help coverage is limited",
                evidence_note="buyers may be unsure about setup effort, integrations, or migration",
            ),
            0.83,
            {
                "weak_prompt_buckets": {"implementation": weak_prompt_buckets.get("implementation", 0.0)},
                "page_coverage": {"docs": _page_type_present(target_site, "docs")},
                "example_prompts": [p["text"] for p in weak_prompts if _coverage_bucket(p) == "implementation"][:3],
            },
        )

    homepage = next((page for page in target_site.get("pages", []) if page.get("page_type") == "homepage"), None)
    weak_homepage = False
    if homepage:
        hero_text = " ".join(
            [
                homepage.get("title", ""),
                homepage.get("meta_description", ""),
                " ".join(homepage.get("headings", [])[:2]),
            ]
        ).strip()
        weak_homepage = len(hero_text.split()) < 10
    else:
        weak_homepage = True

    if weak_homepage or weaknesses.get("informational", 1.0) < 0.6:
        add_recommendation(
            "Strengthen homepage positioning",
            _format_rationale(
                bucket_name="informational",
                bucket_score=weak_prompt_buckets.get("informational", 0.0),
                page_coverage_note="homepage positioning is not stating the offer or buyer clearly enough",
                evidence_note=f"current category signal is {target_distilled.get('product_category') or target_distilled.get('category') or 'unclear'}",
            ),
            0.84,
            {
                "weak_prompt_buckets": {"informational": weak_prompt_buckets.get("informational", 0.0)},
                "page_coverage": {"homepage": _page_type_present(target_site, "homepage")},
                "distilled_category": target_distilled.get("product_category") or target_distilled.get("category"),
            },
        )

    if not recommendations:
        add_recommendation(
            "Expand use-case landing pages around highest-intent workflows",
            _format_rationale(
                bucket_name="use-case",
                bucket_score=weak_prompt_buckets.get("use_case", 0.0),
                page_coverage_note="core page types exist but workflow-specific pages are still thin",
                evidence_note="moderate visibility suggests deeper use-case coverage could unlock more mentions",
            ),
            0.68,
            {
                "weak_prompt_buckets": weak_prompt_buckets,
                "page_coverage": target_site.get("page_type_counts", {}),
                "example_prompts": [p["text"] for p in weak_prompts[:3]],
            },
        )

    recommendations.sort(key=lambda rec: rec["priority_score"], reverse=True)
    return recommendations[:5]


async def generate_content_brief(
    audit: audit_store.AuditState,
    recommendation: dict[str, Any],
) -> dict[str, str]:
    client = GeminiClient()
    weaknesses = _summarize_prompt_weaknesses(audit.prompts)
    weak_bucket_scores = _bucket_average_scores(audit.prompts)
    weakest_prompts = sorted(audit.prompts, key=lambda prompt: prompt.get("score", 0.0))[:5]
    crawl_summary = json.dumps(audit.crawl_summary, indent=2)
    weak_prompt_text = "\n".join(f"- {prompt['text']} ({prompt.get('intent', 'n/a')})" for prompt in weakest_prompts)
    brief_type = _brief_type_for_recommendation(recommendation)

    system_instruction = (
        "You create structured, readable content briefs for SEO and AI-answer visibility work. "
        "Return strict JSON only."
    )
    user_prompt = f"""
Generate a content brief for this recommendation.

Return JSON exactly like:
{{
  "title": "string",
  "body": "markdown string"
}}

Audit domain: {audit.primary_domain}
Industry: {audit.industry or "Unknown"}
Recommendation title: {recommendation['title']}
Recommendation rationale: {recommendation['rationale']}
Weakness summary by intent: {weaknesses}
Weak bucket scores: {weak_bucket_scores}
Recommendation type: {brief_type}
Lowest-scoring prompts:
{weak_prompt_text}

Crawl summary:
{crawl_summary}
"""
    try:
        data = await client.generate_json(
            system_instruction=system_instruction,
            user_prompt=user_prompt,
            temperature=0.5,
        )
        title = str(data.get("title", "")).strip()
        body = str(data.get("body", "")).strip()
        if title and body:
            return {"title": title, "body": body}
    except GeminiError:
        pass

    title = f"Brief: {recommendation['title']}"
    body = _fallback_brief_body(
        audit=audit,
        recommendation=recommendation,
        weakest_prompts=weakest_prompts,
    )
    return {"title": title, "body": body}


async def run_audit(audit_id: str) -> None:
    try:
        state = audit_store.get(audit_id)
        if not state:
            return

        audit_store.update_progress(audit_id, stage="crawling", progress_percent=12)
        target_site = await crawl_site(state.primary_domain)
        competitor_sites = await asyncio.gather(
            *(crawl_site(domain) for domain in state.competitor_domains),
            return_exceptions=True,
        )

        normalized_competitors: list[dict[str, Any]] = []
        for domain, result in zip(state.competitor_domains, competitor_sites, strict=False):
            if isinstance(result, Exception):
                normalized_competitors.append(
                    {
                        "domain": domain,
                        "label": _brand_label(domain),
                        "status": "failed",
                        "error": str(result),
                        "pages": [],
                        "page_type_counts": {},
                        "homepage_summary": "",
                    }
                )
            else:
                normalized_competitors.append(result)

        if not target_site.get("pages"):
            raise RuntimeError(target_site.get("error") or "Target crawl failed")

        audit_store.update_progress(audit_id, stage="generating_prompts", progress_percent=30)
        try:
            await _gemini_with_backoff(
                lambda: _refine_target_category_with_gemini(target_site),
                attempts=2,
                label="GEMINI category refinement",
            )
        except GeminiError as exc:
            print(f"GEMINI category refinement unavailable, keeping heuristic category: {exc}", flush=True)

        audit_store.update_progress(audit_id, stage="generating_prompts", progress_percent=38)
        prompts_source = "gemini"
        try:
            prompts = await _gemini_with_backoff(
                lambda: _generate_prompts_with_gemini(state, target_site, normalized_competitors),
                attempts=3,
                label="GEMINI prompt generation",
            )
        except GeminiError as exc:
            if not _allow_heuristic_fallback():
                raise RuntimeError(
                    "The AI engine could not generate buyer prompts right now (rate limit or "
                    "temporary error). No audit results were produced. Please run the audit "
                    "again in a few minutes."
                ) from exc
            print(f"GEMINI prompt generation failed, using fallback prompts: {exc}", flush=True)
            prompts = _fallback_prompts(state, target_site, normalized_competitors)
            prompts_source = "fallback"

        audit_store.update_progress(audit_id, stage="evaluating", progress_percent=64)
        prompt_results: list[dict[str, Any]] = []
        fallback_evaluations = 0
        for prompt in prompts:
            try:
                result = await _gemini_with_backoff(
                    lambda p=prompt: _evaluate_prompt(
                        prompt=p,
                        target_site=target_site,
                        competitor_sites=normalized_competitors,
                    ),
                    attempts=3,
                    label="GEMINI evaluation",
                )
            except GeminiError as exc:
                if not _allow_heuristic_fallback():
                    raise RuntimeError(
                        "The AI engine was rate-limited mid-audit and could not finish the real "
                        "evaluation. No partial results were produced. Please run the audit "
                        "again in a few minutes."
                    ) from exc
                result = _fallback_evaluation(prompt, target_site, normalized_competitors)
                fallback_evaluations += 1
            prompt_results.append(result)
            await asyncio.sleep(4.0)

        audit_store.update_progress(audit_id, stage="analyzing", progress_percent=86)
        visibility_score, competitor_scores, target_mention_rate, score_components = _build_competitor_scores(
            state,
            prompt_results,
            target_site,
            normalized_competitors,
        )
        recommendations = _generate_recommendations(
            state,
            target_site,
            normalized_competitors,
            prompt_results,
        )

        crawl_summary = {
            "target": {
                "domain": target_site.get("domain"),
                "status": target_site.get("status"),
                "page_type_counts": target_site.get("page_type_counts"),
                "pages_crawled": len(target_site.get("pages", [])),
                "errors": target_site.get("errors", []),
            },
            "competitors": [
                {
                    "domain": site.get("domain"),
                    "status": site.get("status"),
                    "page_type_counts": site.get("page_type_counts"),
                    "pages_crawled": len(site.get("pages", [])),
                    "error": site.get("error"),
                }
                for site in normalized_competitors
            ],
            "prompt_intent_scores": _summarize_prompt_weaknesses(prompt_results),
            "weak_prompt_buckets": _bucket_average_scores(prompt_results),
            "prompt_count": len(prompt_results),
            "prompt_bucket_counts": _bucket_counts(prompt_results),
            "score_components": score_components,
            "category_source": target_site.get("category_source", "heuristic"),
            "prompts_source": prompts_source,
            "fallback_evaluations": fallback_evaluations,
            "degraded": prompts_source == "fallback" or fallback_evaluations > 0,
            "evaluation_note": (
                "Visibility measured from real Gemini answers to each buyer prompt, graded for brand mentions. Headline score uses only prompts that do not name your brand; branded prompts are reported separately. Not a live ChatGPT or Perplexity measurement."
                if prompts_source == "gemini" and fallback_evaluations == 0
                else "Degraded: some results used heuristic estimation because the AI engine was unavailable."
            ),
        }

        audit_store.complete_audit(
            audit_id,
            visibility_score=visibility_score,
            target_mention_rate=target_mention_rate,
            competitor_scores=competitor_scores,
            prompts=prompt_results,
            recommendations=recommendations,
            crawl_summary=crawl_summary,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        audit_store.fail_audit(audit_id, str(exc))
