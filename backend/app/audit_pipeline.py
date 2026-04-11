from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from app import audit_store
from app.services.crawler import crawl_site, distill_site_context
from app.services.gemini_client import GeminiClient, GeminiError

PROMPT_TARGET_MIN = 8
PROMPT_TARGET_MAX = 12
MAX_PROMPT_CHARS = 120
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
        return normalized[:4]

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
    return unique[:4]


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
    secondary_use_case = use_cases[1] if len(use_cases) > 1 else "subscriptions"
    query_category = _category_query_label(category)
    return [
        ("informational", f"best {query_category} for {customer}"),
        ("informational", f"is {brand} good for {primary_use_case}"),
        ("comparative", f"{brand} vs {competitor} for {primary_use_case}"),
        ("comparative", f"{brand} alternatives for {customer}"),
        ("transactional", f"{query_category} with transparent pricing"),
        ("transactional", f"best {query_category} for startups with {secondary_use_case}"),
        ("trust", f"is {brand} trusted for {primary_use_case}"),
        ("trust", f"{query_category} with strong {trust_theme}"),
        ("transactional", f"{brand} pricing for {customer}"),
        ("informational", f"best platform for {primary_use_case}"),
        ("transactional", f"{query_category} easiest to implement"),
        ("informational", f"{competitor} competitors for {primary_use_case}"),
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
        "You generate realistic buyer search prompts for an AI visibility audit. "
        "Return JSON only. Every prompt must be one sentence, human-readable, and under 120 characters. "
        "Do not paste source text, titles, menus, taglines, or paragraphs from the website. "
        "Make prompts specific to buyer use cases, evaluation, pricing, implementation, trust, and competitor comparison. "
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
- Avoid placeholders like "your market", "your product", or generic filler.
- Avoid raw nav/menu text such as "Products Solutions Developers Resources Pricing".
- Avoid pasting titles with separators like "|" or long taglines.
- Include competitor names when useful.
- Prefer concrete buyer wording like "vs", "pricing", "alternatives", "for SaaS billing", "for marketplaces", "easy to implement".
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
    cleaned = _ensure_prompt_coverage(
        cleaned,
        state=state,
        target_distilled=target_distilled,
        competitor_distilled=competitor_distilled,
    )
    if len(cleaned) < PROMPT_TARGET_MIN:
        raise GeminiError("Prompt generation returned too few usable prompts after backfill")
    return cleaned[:PROMPT_TARGET_MAX]


async def _evaluate_prompt(
    *,
    prompt: dict[str, Any],
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> dict[str, Any]:
    client = GeminiClient()
    target_distilled = distill_site_context(target_site)
    competitor_distilled = [distill_site_context(site) for site in competitor_sites if site.get("domain")]
    target_label = target_site.get("label") or _brand_label(target_site.get("domain", ""))
    competitor_labels = [
        site.get("label") or _brand_label(site.get("domain", ""))
        for site in competitor_sites
        if site.get("domain")
    ]
    system_instruction = (
        "You simulate a customer-facing AI assistant answer, then grade whether the target brand "
        "earned visibility in that answer. Return strict JSON only."
    )
    user_prompt = f"""
Prompt from a prospective customer:
{prompt['text']}

Target brand: {target_label}
Competitor brands: {competitor_labels}

Use the distilled site context below to answer as a helpful AI assistant. Then evaluate the target brand's visibility.

Return JSON exactly like:
{{
  "answer": "string",
  "target_role": "central|supporting|absent",
  "competitor_mentions": ["Brand A"],
  "competitor_role": "none|supporting|strong",
  "fit_score": 0.0,
  "explanation": "string"
}}

Rules:
- "target_role" is central if the target is a top recommendation or best fit.
- "target_role" is supporting if the target is mentioned positively but is not the main answer.
- "target_role" is absent if not mentioned.
- "competitor_role" is strong if competitors dominate the answer, supporting if they are mentioned but not dominant, none otherwise.
- "fit_score" must be between 0 and 1 based on how well the target matches the prompt.

Primary company context:
{json.dumps(target_distilled, ensure_ascii=True)}

Competitor context:
{json.dumps(competitor_distilled, ensure_ascii=True)}
"""
    data = await client.generate_json(
        system_instruction=system_instruction,
        user_prompt=user_prompt,
        temperature=0.3,
    )

    target_role = str(data.get("target_role", "absent")).strip().lower()
    competitor_role = str(data.get("competitor_role", "none")).strip().lower()
    if target_role not in {"central", "supporting", "absent"}:
        raise GeminiError("Gemini returned an invalid target role")
    if competitor_role not in {"none", "supporting", "strong"}:
        competitor_role = "none"

    raw_strength = data.get("fit_score", 0.0)
    try:
        fit_score = float(raw_strength)
    except (TypeError, ValueError) as exc:
        raise GeminiError("Gemini returned a non-numeric fit score") from exc
    fit_score = max(0.0, min(1.0, fit_score))

    competitor_mentions = data.get("competitor_mentions", [])
    if not isinstance(competitor_mentions, list):
        competitor_mentions = []

    score = 0.0
    if target_role == "central":
        score = 0.82 + (fit_score * 0.16)
        if competitor_role == "strong":
            score -= 0.14
        elif competitor_role == "supporting":
            score -= 0.06
    elif target_role == "supporting":
        score = 0.48 + (fit_score * 0.24)
        if competitor_role == "strong":
            score -= 0.12
        elif competitor_role == "supporting":
            score -= 0.04

    mention_strength = round(max(0.0, min(1.0, score)), 2)
    target_mentioned = target_role != "absent"

    return {
        "id": prompt["id"],
        "text": prompt["text"],
        "intent": prompt.get("intent"),
        "mentioned": target_mentioned,
        "score": round(mention_strength, 2),
        "explanation": str(data.get("explanation", "")).strip() or "No explanation returned.",
        "competitor_mentions": [str(item).strip() for item in competitor_mentions if str(item).strip()],
        "answer": str(data.get("answer", "")).strip(),
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
    if competitor_mentions:
        strength -= min(0.1, 0.04 * len(competitor_mentions))
    strength = round(max(0.0, min(1.0, strength)), 2)

    mentioned = strength > 0
    return {
        "id": prompt["id"],
        "text": prompt["text"],
        "intent": prompt.get("intent"),
        "mentioned": mentioned,
        "score": round(strength, 2),
        "explanation": "Fallback heuristic based on page-type coverage, prompt intent, and competitor presence.",
        "competitor_mentions": competitor_mentions,
        "answer": "",
    }


def _build_competitor_scores(
    state: audit_store.AuditState,
    prompts: list[dict[str, Any]],
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]], float]:
    total_prompts = max(len(prompts), 1)
    target_total = sum(float(prompt.get("score", 0.0)) for prompt in prompts)
    target_mention_rate = round(
        _safe_ratio(sum(1 for prompt in prompts if prompt.get("mentioned")), total_prompts),
        2,
    )
    overall_score = round(_safe_ratio(target_total, total_prompts) * 100, 1)

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

    return overall_score, scores, target_mention_rate


def _generate_recommendations(
    state: audit_store.AuditState,
    target_site: dict[str, Any],
    competitor_sites: list[dict[str, Any]],
    prompts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    weaknesses = _summarize_prompt_weaknesses(prompts)
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

    def add_recommendation(title: str, rationale: str, priority_score: float) -> None:
        recommendations.append(
            {
                "id": f"rec-{state.id[:8]}-{len(recommendations)}",
                "title": title,
                "rationale": rationale,
                "priority_score": round(priority_score, 2),
                "brief": None,
            }
        )

    if not _page_type_present(target_site, "faq"):
        add_recommendation(
            "Build a buyer FAQ and objections page",
            "The crawl did not find an FAQ/help page, and weaker informational prompts suggest buyers are not getting direct answers to evaluation questions.",
            0.93 if weaknesses.get("informational", 1.0) < 0.55 else 0.79,
        )
    if not _page_type_present(target_site, "comparison") or comparison_weak:
        competitor_has_comparison = any(_page_type_present(site, "comparison") for site in competitor_sites)
        add_recommendation(
            f"Strengthen comparison coverage against {primary_competitor}",
            f"Comparison-style prompts underperform, including queries like: {weak_prompt_text}. "
            + ("Competitors already appear to have comparison content. " if competitor_has_comparison else "")
            + "The target site needs clearer alternative/comparison pages for buyers evaluating options.",
            0.96 if weaknesses.get("comparative", 1.0) < 0.6 else 0.84,
        )
    if not _page_type_present(target_site, "pricing") or pricing_weak:
        add_recommendation(
            "Clarify pricing and packaging for evaluators",
            "Pricing-oriented prompts are weak, and the crawl suggests buyers may not be getting enough clarity on plans, costs, or fit before contacting sales.",
            0.9 if weaknesses.get("transactional", 1.0) < 0.62 else 0.78,
        )
    if not _page_type_present(target_site, "reviews") or trust_weak:
        add_recommendation(
            "Expand trust content with proof and customer evidence",
            "Trust and review-oriented prompts are underperforming, which usually means the site needs stronger proof points such as case studies, testimonials, security/compliance detail, or customer outcomes.",
            0.88 if weaknesses.get("trust", 1.0) < 0.62 else 0.75,
        )
    if implementation_weak and not _page_type_present(target_site, "docs"):
        add_recommendation(
            "Add onboarding and implementation content",
            "Implementation-style prompts are weak and the crawl found limited docs/help content, making it harder for buyers to picture setup effort and integrations.",
            0.83,
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
            f"The homepage crawl suggests the primary offer, category, or buyer is not stated clearly enough. Distilled category signal currently looks like: {target_distilled.get('product_category') or target_distilled.get('category') or 'unclear'}.",
            0.84,
        )

    if not recommendations:
        add_recommendation(
            "Expand use-case landing pages around highest-intent workflows",
            "Core page types exist, but the next gain is deeper use-case content tied to the buyer workflows showing only moderate simulated visibility.",
            0.68,
        )

    recommendations.sort(key=lambda rec: rec["priority_score"], reverse=True)
    return recommendations[:5]


async def generate_content_brief(
    audit: audit_store.AuditState,
    recommendation: dict[str, Any],
) -> dict[str, str]:
    client = GeminiClient()
    weaknesses = _summarize_prompt_weaknesses(audit.prompts)
    weakest_prompts = sorted(audit.prompts, key=lambda prompt: prompt.get("score", 0.0))[:5]
    crawl_summary = json.dumps(audit.crawl_summary, indent=2)
    weak_prompt_text = "\n".join(f"- {prompt['text']} ({prompt.get('intent', 'n/a')})" for prompt in weakest_prompts)

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
    body = (
        f"## Objective\n\n"
        f"Improve simulated visibility for `{audit.primary_domain}` by shipping content aligned to **{recommendation['title'].lower()}**.\n\n"
        f"## Why this matters\n\n"
        f"{recommendation['rationale']}\n\n"
        f"## Weak prompt themes\n\n"
        + "\n".join(f"- {prompt['text']}" for prompt in weakest_prompts)
        + "\n\n## Suggested sections\n\n"
        f"1. Clear positioning for the buyer and use case.\n"
        f"2. Comparison or evaluation criteria buyers actually ask about.\n"
        f"3. Evidence: proof points, testimonials, or outcomes.\n"
        f"4. FAQs that answer objections.\n"
        f"5. CTA to start a demo, trial, or contact flow.\n"
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

        audit_store.update_progress(audit_id, stage="generating_prompts", progress_percent=38)
        try:
            prompts = await _generate_prompts_with_gemini(state, target_site, normalized_competitors)
        except GeminiError:
            prompts = _fallback_prompts(state, target_site, normalized_competitors)

        audit_store.update_progress(audit_id, stage="evaluating", progress_percent=64)
        prompt_results: list[dict[str, Any]] = []
        for prompt in prompts:
            try:
                result = await _evaluate_prompt(
                    prompt=prompt,
                    target_site=target_site,
                    competitor_sites=normalized_competitors,
                )
            except GeminiError:
                result = _fallback_evaluation(prompt, target_site, normalized_competitors)
            prompt_results.append(result)

        audit_store.update_progress(audit_id, stage="analyzing", progress_percent=86)
        visibility_score, competitor_scores, target_mention_rate = _build_competitor_scores(
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
            "prompt_count": len(prompt_results),
            "prompt_bucket_counts": {
                bucket: sum(1 for prompt in prompt_results if _coverage_bucket(prompt) == bucket)
                for bucket in PROMPT_BUCKETS
            },
            "evaluation_note": "Directional simulated estimate grounded in crawled pages and Gemini-generated answers.",
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
