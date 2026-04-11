from __future__ import annotations

from collections import deque
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import re

import httpx
from bs4 import BeautifulSoup


PAGE_TYPE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("pricing", ("pricing", "plans", "plan", "cost")),
    ("faq", ("faq", "frequently-asked", "help-center", "questions")),
    ("comparison", ("compare", "comparison", "versus", "-vs-", "/vs/")),
    ("reviews", ("reviews", "testimonials", "customers", "case-study", "case-studies")),
    ("docs", ("docs", "documentation", "help", "support", "knowledge-base")),
    ("about", ("about", "company", "team", "mission")),
    ("contact", ("contact", "demo", "sales")),
    ("blog", ("blog", "resources", "articles", "guides")),
]

NAVIGATION_TERMS = {
    "products",
    "product",
    "solutions",
    "developers",
    "resources",
    "pricing",
    "docs",
    "documentation",
    "login",
    "log in",
    "sign in",
    "start now",
    "book demo",
    "request demo",
    "contact sales",
    "get started",
    "customers",
    "company",
    "about",
    "support",
}

PROMO_PHRASES = (
    "to grow your revenue",
    "grow your revenue",
    "grow faster",
    "accelerate new business opportunities",
    "new business opportunities",
    "for businesses of all sizes",
    "for businesses",
    "for modern businesses",
    "millions of companies use",
    "millions of businesses use",
    "unified platform",
    "all in one",
)

CATEGORY_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("payment processor", "payment processing", "accept payments", "online payments"), "payment processing"),
    (("subscription billing", "subscriptions", "billing"), "billing and subscriptions"),
    (("checkout", "online payments"), "online payments"),
    (("marketplace payments", "marketplace"), "marketplace payments"),
    (("financial infrastructure",), "financial infrastructure for businesses"),
]

USE_CASE_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("subscription", "subscriptions", "billing"), "subscription billing"),
    (("marketplace",), "marketplaces"),
    (("ecommerce", "online store", "checkout"), "ecommerce payments"),
    (("api", "developer"), "payments APIs"),
    (("invoice", "invoic"), "invoicing"),
]


def normalize_domain(raw: str) -> str:
    domain = raw.strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://")
    domain = domain.split("/")[0].split("?")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _domain_label(domain: str) -> str:
    return normalize_domain(domain).split(".")[0].replace("-", " ").title()


def _clean_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _normalize_fragment(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in value)
    return " ".join(cleaned.split())


def _is_navigation_fragment(text: str) -> bool:
    lowered = _normalize_fragment(text)
    if not lowered:
        return True
    if lowered in NAVIGATION_TERMS:
        return True
    parts = lowered.split()
    if 1 <= len(parts) <= 5 and all(part in NAVIGATION_TERMS for part in parts):
        return True
    return False


def _dedupe_fragments(fragments: list[str], *, min_chars: int = 0) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for fragment in fragments:
        cleaned = _clean_text(fragment)
        normalized = _normalize_fragment(cleaned)
        if len(cleaned) < min_chars or normalized in seen or _is_navigation_fragment(cleaned):
            continue
        seen.add(normalized)
        results.append(cleaned)
    return results


def _split_sentences(text: str) -> list[str]:
    normalized = text.replace("?", ".").replace("!", ".")
    parts = [part.strip(" -|,;:") for part in normalized.split(".")]
    return [part for part in parts if part]


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _strip_promo_phrases(text: str) -> str:
    cleaned = text
    for phrase in PROMO_PHRASES:
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-|:")
    return cleaned


def _normalize_category_label(text: str) -> str:
    lowered = _normalize_fragment(_strip_promo_phrases(text))
    if not lowered:
        return ""
    for keywords, label in CATEGORY_HINTS:
        if any(keyword in lowered for keyword in keywords):
            return label
    words = lowered.split()
    if 2 <= len(words) <= 5:
        return " ".join(words)
    return ""


def _normalize_use_case_label(text: str) -> str:
    lowered = _normalize_fragment(_strip_promo_phrases(text))
    if not lowered:
        return ""
    for keywords, label in USE_CASE_HINTS:
        if any(keyword in lowered for keyword in keywords):
            return label
    return ""


def _looks_like_boilerplate(text: str) -> bool:
    lowered = _normalize_fragment(text)
    if not lowered:
        return True
    words = lowered.split()
    if len(words) <= 2 and any(word in NAVIGATION_TERMS for word in words):
        return True
    if len(set(words)) <= 2 and len(words) > 5:
        return True
    if "|" in text and len(text) > 60:
        return True
    if any(phrase in lowered for phrase in ("grow your revenue", "new business opportunities")):
        return True
    return False


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).strip()


def classify_page_type(url: str, title: str, headings: list[str]) -> str:
    parsed = urlparse(url)
    haystack = " ".join(
        [
            parsed.path.lower(),
            title.lower(),
            " ".join(h.lower() for h in headings),
        ]
    )
    if parsed.path in ("", "/"):
        return "homepage"
    for page_type, patterns in PAGE_TYPE_PATTERNS:
        if any(pattern in haystack for pattern in patterns):
            return page_type
    return "general"


async def _can_fetch(base_url: str, target_url: str) -> bool:
    robots_url = urljoin(base_url, "/robots.txt")
    parser = RobotFileParser()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(robots_url)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch("*", target_url)
    except Exception:
        return True


async def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; VisibilityAuditor/1.0; "
            "+https://localhost)"
        )
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _extract_page(url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    meta = ""
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag and meta_tag.get("content"):
        meta = _clean_text(str(meta_tag["content"]))

    headings: list[str] = []
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name, limit=8):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text:
                headings.append(text)

    raw_fragments = [_clean_text(value) for value in soup.stripped_strings]
    useful_fragments = _dedupe_fragments(
        [fragment for fragment in raw_fragments if not _looks_like_boilerplate(fragment)],
        min_chars=20,
    )
    body_text = _clean_text(" ".join(useful_fragments[:80]))
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        links.append(urljoin(url, href))

    page_type = classify_page_type(url, title, headings)
    return {
        "url": url,
        "title": title,
        "meta_description": meta,
        "headings": headings[:10],
        "content_text": _truncate_words(body_text, 500),
        "word_count": len(body_text.split()),
        "page_type": page_type,
        "links": links,
    }


def distill_site_context(site: dict[str, Any]) -> dict[str, Any]:
    pages = site.get("pages", [])
    label = site.get("label") or _domain_label(site.get("domain", ""))
    if not pages:
        return {
            "domain": site.get("domain"),
            "label": label,
            "summary": "",
            "category": "",
            "keywords": [],
            "page_type_counts": site.get("page_type_counts", {}),
            "page_highlights": [],
        }

    homepage = next((page for page in pages if page.get("page_type") == "homepage"), pages[0])
    candidate_fragments: list[str] = []
    candidate_fragments.extend([homepage.get("meta_description", ""), homepage.get("title", "")])
    candidate_fragments.extend(homepage.get("headings", [])[:3])
    candidate_fragments.extend(_split_sentences(homepage.get("content_text", ""))[:8])

    useful = _dedupe_fragments(
        [fragment for fragment in candidate_fragments if fragment and not _looks_like_boilerplate(fragment)],
        min_chars=12,
    )
    cleaned_useful = _dedupe_fragments(
        [_strip_promo_phrases(fragment) for fragment in useful if _strip_promo_phrases(fragment)],
        min_chars=8,
    )

    summary_parts: list[str] = []
    for fragment in cleaned_useful:
        if len(" ".join(summary_parts + [fragment])) > 220:
            continue
        summary_parts.append(fragment)
        if len(summary_parts) >= 3:
            break
    summary = ". ".join(summary_parts).strip(" .")

    category = ""
    for fragment in useful:
        lowered = fragment.lower()
        if label.lower() in lowered and len(fragment.split()) >= 4:
            category = fragment
            break
    if not category and cleaned_useful:
        category = cleaned_useful[0]

    page_highlights: list[str] = []
    for page in pages[:6]:
        title = page.get("title", "")
        if title and not _looks_like_boilerplate(title):
            page_highlights.append(f"{page.get('page_type')}: {title[:90]}")
    page_highlights = _dedupe_fragments(page_highlights, min_chars=8)[:5]

    keyword_pool = _dedupe_fragments(
        useful + [page.get("title", "") for page in pages[:5]] + homepage.get("headings", [])[:4],
        min_chars=4,
    )
    keywords: list[str] = []
    for fragment in keyword_pool:
        lowered = fragment.lower()
        if label.lower() in lowered:
            continue
        if len(fragment) > 40:
            continue
        keywords.append(fragment)
        if len(keywords) >= 6:
            break

    all_fragments = _dedupe_fragments(
        useful
        + [page.get("meta_description", "") for page in pages[:6]]
        + [page.get("title", "") for page in pages[:6]]
        + [heading for page in pages[:6] for heading in page.get("headings", [])[:4]],
        min_chars=8,
    )

    audiences: list[str] = []
    use_cases: list[str] = []
    trust_signals: list[str] = []
    for fragment in all_fragments:
        lowered = fragment.lower()
        if any(term in lowered for term in ("startup", "small business", "enterprise", "developer", "platform", "marketplace", "saas", "ecommerce", "team", "businesses")):
            audiences.append(fragment[:80])
        if any(term in lowered for term in ("subscription", "billing", "payments", "checkout", "marketplace", "invoic", "fraud", "onboard", "api", "automation", "implementation")):
            use_cases.append(fragment[:90])
        if any(term in lowered for term in ("trusted", "trust", "security", "compliance", "review", "customers", "case stud", "testimonial", "proof", "reliable")):
            trust_signals.append(fragment[:90])

    audiences = _dedupe_fragments(audiences, min_chars=6)[:4]
    use_cases = _dedupe_fragments(use_cases, min_chars=6)[:6]
    trust_signals = _dedupe_fragments(trust_signals, min_chars=6)[:5]

    normalized_categories: list[str] = []
    normalized_use_cases: list[str] = []
    for fragment in all_fragments:
        category_label = _normalize_category_label(fragment)
        if category_label:
            normalized_categories.append(category_label)
        use_case_label = _normalize_use_case_label(fragment)
        if use_case_label:
            normalized_use_cases.append(use_case_label)

    normalized_categories = _dedupe_fragments(normalized_categories, min_chars=6)
    normalized_use_cases = _dedupe_fragments(normalized_use_cases, min_chars=4)

    product_category = normalized_categories[0] if normalized_categories else _normalize_category_label(category) or category[:120]
    normalized_trust = "customer proof"
    for candidate in trust_signals:
        lowered = candidate.lower()
        if "compliance" in lowered or "security" in lowered:
            normalized_trust = "security and compliance"
            break
        if "testimonial" in lowered or "case stud" in lowered or "customer" in lowered:
            normalized_trust = "customer proof"
            break
        if "trusted" in lowered or "reliable" in lowered:
            normalized_trust = "reliability"
            break

    return {
        "domain": site.get("domain"),
        "label": label,
        "summary": summary,
        "category": product_category,
        "raw_category": category[:120],
        "product_category": product_category,
        "normalized_category": product_category,
        "audiences": audiences,
        "use_cases": use_cases,
        "normalized_use_cases": normalized_use_cases,
        "trust_signals": trust_signals,
        "normalized_trust": normalized_trust,
        "keywords": keywords,
        "page_type_counts": site.get("page_type_counts", {}),
        "page_highlights": page_highlights,
    }


def _prioritize_links(domain: str, page: dict[str, Any]) -> list[str]:
    root = normalize_domain(domain)
    candidates: list[tuple[int, str]] = []
    for link in page.get("links", []):
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        host = normalize_domain(parsed.netloc)
        if host != root:
            continue
        path = parsed.path.lower() or "/"
        score = 50
        if path == "/":
            score -= 40
        for idx, (_, patterns) in enumerate(PAGE_TYPE_PATTERNS, start=1):
            if any(pattern in path for pattern in patterns):
                score = min(score, idx)
        candidates.append((score, link.split("#")[0]))

    unique: list[str] = []
    seen: set[str] = set()
    for _, link in sorted(candidates, key=lambda item: (item[0], item[1])):
        if link not in seen:
            unique.append(link)
            seen.add(link)
    return unique


async def crawl_site(domain: str, *, max_pages: int = 6) -> dict[str, Any]:
    normalized = normalize_domain(domain)
    homepage_candidates = [f"https://{normalized}", f"http://{normalized}"]
    last_error: str | None = None
    start_url: str | None = None

    for candidate in homepage_candidates:
        if not await _can_fetch(candidate, candidate):
            last_error = f"robots.txt disallows crawling {candidate}"
            continue
        try:
            await _fetch_html(candidate)
            start_url = candidate
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    if not start_url:
        return {
            "domain": normalized,
            "label": _domain_label(normalized),
            "status": "failed",
            "error": last_error or "Could not fetch homepage",
            "pages": [],
            "page_type_counts": {},
            "homepage_summary": "",
        }

    queue: deque[str] = deque([start_url])
    seen: set[str] = set()
    pages: list[dict[str, Any]] = []
    errors: list[str] = []

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if not await _can_fetch(start_url, url):
            errors.append(f"Robots blocked {url}")
            continue

        try:
            html = await _fetch_html(url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            continue

        page = _extract_page(url, html)
        pages.append(page)
        for link in _prioritize_links(normalized, page):
            if link not in seen and len(queue) + len(pages) < max_pages * 3:
                queue.append(link)

    page_type_counts: dict[str, int] = {}
    for page in pages:
        page_type_counts[page["page_type"]] = page_type_counts.get(page["page_type"], 0) + 1

    homepage = next((page for page in pages if page["page_type"] == "homepage"), pages[0] if pages else None)
    homepage_summary = ""
    if homepage:
        homepage_summary = _truncate_words(
            " ".join(
                value
                for value in [
                    homepage.get("title", ""),
                    homepage.get("meta_description", ""),
                    " ".join(homepage.get("headings", [])),
                    homepage.get("content_text", ""),
                ]
                if value
            ),
            140,
        )

    return {
        "domain": normalized,
        "label": _domain_label(normalized),
        "status": "completed" if pages else "failed",
        "error": None if pages else (errors[0] if errors else "No pages crawled"),
        "pages": pages,
        "page_type_counts": page_type_counts,
        "homepage_summary": homepage_summary,
        "errors": errors[:10],
    }


def build_site_context(site: dict[str, Any]) -> str:
    pages = site.get("pages", [])
    if not pages:
        return f"Domain: {site.get('domain')}\nNo crawlable pages were captured."

    distilled = distill_site_context(site)
    lines = [
        f"Domain: {site.get('domain')}",
        f"Brand label: {distilled.get('label')}",
        f"Company summary: {distilled.get('summary') or 'N/A'}",
        f"Category hint: {distilled.get('category') or 'N/A'}",
        f"Keywords: {', '.join(distilled.get('keywords', [])) or 'N/A'}",
        f"Page type counts: {distilled.get('page_type_counts')}",
        "Key pages:",
    ]
    for page in pages[:4]:
        lines.append(
            (
                f"- [{page['page_type']}] {page['url']}\n"
                f"  Title: {page['title'] or 'N/A'}\n"
                f"  Meta: {page['meta_description'] or 'N/A'}\n"
                f"  Headings: {', '.join(page['headings'][:3]) or 'N/A'}"
            )
        )
    return "\n".join(lines)
