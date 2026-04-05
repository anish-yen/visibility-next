"""
MVP crawl service: sitemap or homepage nav, robots.txt, httpx + BeautifulSoup.
See CLAUDE.md §7.2 Crawl Service.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = "VisibilityAuditorBot/0.1"
MAX_PAGES = 20
RATE_DELAY_SEC = 1.0
MAX_CANDIDATE_URLS = 40
REQUEST_TIMEOUT = 25.0
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")

_NS_STRIP = re.compile(r"^\{[^}]+\}")


def _local_tag(tag: str) -> str:
    return _NS_STRIP.sub("", tag)


def _normalize_domain(domain: str) -> str:
    d = domain.strip().lower()
    d = d.removeprefix("https://").removeprefix("http://")
    d = d.split("/")[0].split("?")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _origin_for_domain(bare_domain: str) -> str:
    return f"https://{bare_domain}"


def _same_site(url: str, bare_domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    bd = bare_domain.lower()
    return host == bd or host == f"www.{bd}"


def _normalize_url(url: str, base: str, bare_domain: str) -> str | None:
    joined = urljoin(base, url)
    parsed = urlparse(joined)
    if parsed.scheme not in ("http", "https"):
        return None
    if not _same_site(joined, bare_domain):
        return None
    path = parsed.path or "/"
    clean = urlunparse(
        (parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, "")
    )
    return clean


def classify_page_type(url: str) -> str:
    path = urlparse(url).path.lower().rstrip("/") or "/"
    if path == "/":
        return "homepage"
    if "/pricing" in path or path.endswith("/pricing"):
        return "pricing"
    if "/faq" in path or "/help" in path:
        return "faq"
    if "/blog" in path or "/resources" in path:
        return "blog"
    if "/compare" in path or "/vs" in path:
        return "comparison"
    if "/reviews" in path or "/testimonials" in path:
        return "review"
    if "/about" in path or "/contact" in path:
        return "about"
    return "other"


async def _load_robots(client: httpx.AsyncClient, origin: str) -> RobotFileParser | None:
    robots_url = urljoin(origin, "/robots.txt")
    try:
        r = await client.get(robots_url, follow_redirects=True)
        if r.status_code != 200:
            logger.info("robots.txt not found or HTTP %s for %s", r.status_code, robots_url)
            return None
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp
    except Exception as e:
        logger.warning("robots.txt fetch failed for %s: %s", robots_url, e)
        return None


def _robots_allows(rp: RobotFileParser | None, url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _parse_sitemap_xml(content: str) -> tuple[list[str], bool]:
    """
    Returns (urls, is_index) where is_index means sitemap index (nested sitemaps).
    """
    urls: list[str] = []
    is_index = False
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], False

    root_tag = _local_tag(root.tag).lower()
    if root_tag == "sitemapindex":
        is_index = True
        for el in root.iter():
            if _local_tag(el.tag).lower() == "loc" and el.text:
                urls.append(el.text.strip())
    elif root_tag == "urlset":
        for el in root.iter():
            if _local_tag(el.tag).lower() == "loc" and el.text:
                urls.append(el.text.strip())
    return urls, is_index


async def _fetch_sitemap_urls(client: httpx.AsyncClient, origin: str) -> list[str]:
    collected: list[str] = []
    for path in SITEMAP_PATHS:
        sm_url = urljoin(origin, path)
        try:
            r = await client.get(sm_url, follow_redirects=True)
            if r.status_code != 200:
                continue
            urls, is_index = _parse_sitemap_xml(r.text)
            if not urls:
                continue
            if is_index:
                for nested in urls[:5]:
                    try:
                        nr = await client.get(nested, follow_redirects=True)
                        if nr.status_code != 200:
                            continue
                        inner, _ = _parse_sitemap_xml(nr.text)
                        collected.extend(inner)
                    except Exception as e:
                        logger.warning("Nested sitemap failed %s: %s", nested, e)
            else:
                collected.extend(urls)
            if collected:
                break
        except Exception as e:
            logger.warning("Sitemap fetch failed %s: %s", sm_url, e)
    return collected


def _extract_nav_links(html: str, origin: str, bare_domain: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[str] = []

    def push(href: str | None) -> None:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        nu = _normalize_url(href, origin, bare_domain)
        if nu and nu not in seen:
            seen.add(nu)
            out.append(nu)

    for nav in soup.find_all("nav"):
        for a in nav.find_all("a", href=True):
            push(a.get("href"))
    if len(out) < 5:
        for hdr in soup.find_all(["header", "footer"]):
            for a in hdr.find_all("a", href=True):
                push(a.get("href"))
    if len(out) < 5:
        for a in soup.find_all("a", href=True)[:80]:
            push(a.get("href"))
    return out


def _extract_page_fields(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""

    meta_desc = None
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if md and md.get("content"):
        meta_desc = md["content"].strip()
    if not meta_desc:
        og = soup.find("meta", property=re.compile(r"^og:description$", re.I))
        if og and og.get("content"):
            meta_desc = og["content"].strip()

    headings_json: list[dict[str, str]] = []
    for tag_name in ("h1", "h2", "h3"):
        for h in soup.find_all(tag_name):
            t = h.get_text(separator=" ", strip=True)
            if t:
                headings_json.append({"level": tag_name, "text": t})

    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    body = soup.find("body")
    content_text = body.get_text(separator="\n", strip=True) if body else ""
    content_text = re.sub(r"\n{3,}", "\n\n", content_text)
    word_count = len(content_text.split()) if content_text else 0

    return {
        "title": title,
        "meta_description": meta_desc,
        "headings_json": headings_json,
        "content_text": content_text[:500_000],
        "word_count": word_count,
    }


def _page_record(
    *,
    bare_domain: str,
    url: str,
    page_type: str,
    crawl_status: str,
    robots_blocked: bool,
    **fields: object,
) -> dict:
    rec = {
        "id": None,
        "audit_id": None,
        "domain": bare_domain,
        "url": url,
        "page_type": page_type,
        "title": fields.get("title", ""),
        "meta_description": fields.get("meta_description"),
        "headings_json": fields.get("headings_json", []),
        "content_text": fields.get("content_text", ""),
        "word_count": fields.get("word_count", 0),
        "crawl_status": crawl_status,
        "robots_blocked": robots_blocked,
    }
    return rec


async def crawl_site(domain: str) -> list[dict]:
    """
    Crawl up to MAX_PAGES HTML pages for a domain (MVP).
    Returns Page-shaped dicts per CLAUDE.md (id/audit_id null).
    """
    bare = _normalize_domain(domain)
    origin = _origin_for_domain(bare)
    homepage = urljoin(origin, "/")

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as client:
        rp = await _load_robots(client, origin)

        candidates: list[str] = []
        seen: set[str] = set()

        def add(u: str | None) -> None:
            if not u:
                return
            nu = _normalize_url(u, origin, bare)
            if nu and nu not in seen:
                seen.add(nu)
                candidates.append(nu)

        add(homepage)

        sm_urls = await _fetch_sitemap_urls(client, origin)
        for u in sm_urls:
            add(u)
            if len(candidates) >= MAX_CANDIDATE_URLS:
                break

        precrawled: dict[str, str] = {}
        if len(candidates) <= 1:
            try:
                await asyncio.sleep(RATE_DELAY_SEC)
                if not _robots_allows(rp, homepage):
                    logger.info("Homepage disallowed by robots, skipping nav discovery")
                else:
                    hr = await client.get(homepage)
                    hr.raise_for_status()
                    precrawled[homepage] = hr.text
                    for link in _extract_nav_links(hr.text, origin, bare):
                        add(link)
                        if len(candidates) >= MAX_CANDIDATE_URLS:
                            break
            except Exception as e:
                logger.warning("Homepage fetch for nav links failed: %s", e)

        pages: list[dict] = []

        for url in candidates:
            if len(pages) >= MAX_PAGES:
                break

            await asyncio.sleep(RATE_DELAY_SEC)

            if not _robots_allows(rp, url):
                logger.info("Skipping (robots.txt): %s", url)
                continue

            try:
                if url in precrawled:
                    html = precrawled.pop(url)
                    resolved_url = url
                else:
                    r = await client.get(url)
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and "html" not in ctype:
                        logger.info("Skipping non-HTML %s: %s", ctype, url)
                        continue
                    html = r.text
                    resolved_url = str(r.url)
                fields = _extract_page_fields(html, resolved_url)
                pages.append(
                    _page_record(
                        bare_domain=bare,
                        url=resolved_url,
                        page_type=classify_page_type(resolved_url),
                        crawl_status="ok",
                        robots_blocked=False,
                        **fields,
                    )
                )
            except Exception as e:
                logger.warning("Page fetch failed %s: %s", url, e)
                continue

    return pages
