"""Real answer-engine runners, zero-budget edition.

Replaces the simulated Gemini role-play in audit_pipeline._evaluate_prompt with
actual calls to answer engines that buyers use. Each runner returns the answer
text AND the URLs the engine cited, which feeds the citation map and rewrite
engine.

Cost tiers:
  FREE (works today, no new keys):
    - "gemini":   Gemini + Google Search grounding. Uses the GEMINI_API_KEY
                  already in backend/.env. Grounded answers come back with
                  groundingMetadata -> real cited URLs. Measures the
                  Google AI / Gemini lane, which is also a strong proxy for
                  what AI Overviews cite.
    - "chatgpt_web" / "perplexity_web": STUBS. Free path to the ChatGPT and
                  Perplexity lanes is browser automation of the logged-out web
                  UIs (no API key exists for those answers). The stub defines
                  the exact interface; plug in a cloud-browser implementation
                  later without touching the loop.
  PAID UPGRADES (optional, only used if the env var is set):
    - "perplexity": Perplexity Sonar API (PERPLEXITY_API_KEY)
    - "openai":     OpenAI Responses API + web_search (OPENAI_API_KEY)

Every runner degrades to EngineAnswer(error=...) instead of raising through
run_engines(), so one dead engine never kills a cycle.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass
class EngineAnswer:
    engine: str                       # "gemini" | "perplexity" | "openai" | "chatgpt_web" | ...
    prompt_text: str
    answer_text: str
    cited_urls: list[str] = field(default_factory=list)
    mentioned_domains: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)  # what the engine actually searched
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    error: str | None = None


class EngineError(RuntimeError):
    pass


def _domain_of(url: str) -> str:
    m = re.sub(r"^https?://", "", url.strip()).split("/")[0].lower()
    return m[4:] if m.startswith("www.") else m


def detect_mentions(answer: EngineAnswer, brand_labels: list[str], domains: list[str]) -> EngineAnswer:
    haystacks = [answer.answer_text.lower()]
    haystacks.extend(u.lower() for u in answer.cited_urls)
    hits: set[str] = set()
    for label, domain in zip(brand_labels, domains, strict=False):
        label_l, domain_l = label.lower().strip(), domain.lower().strip()
        if (label_l and any(label_l in h for h in haystacks)) or (
            domain_l and any(domain_l in h for h in haystacks)
        ):
            hits.add(domain_l or label_l)
    answer.mentioned_domains = sorted(hits)
    return answer


# --------------------------------------------------------------------------
# FREE TIER: Gemini + Google Search grounding
# --------------------------------------------------------------------------

async def run_gemini_grounded(prompt_text: str, *, timeout: float = 60.0) -> EngineAnswer:
    """One buyer prompt against Gemini with Google Search grounding.

    Uses the existing GEMINI_API_KEY. Set GEMINI_MODEL=gemini-2.0-flash (or
    newer) in backend/.env: grounding needs a 2.x model with the
    google_search tool. We try google_search first and fall back to the 1.5
    google_search_retrieval shape so nothing breaks on his current default.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EngineError("GEMINI_API_KEY is not configured")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"{GEMINI_BASE}/{model}:generateContent"

    async def _call(tool_payload: dict[str, Any]) -> httpx.Response:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "tools": [tool_payload],
            "generationConfig": {"temperature": 0.2},
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, params={"key": api_key}, json=payload)

    resp = await _call({"google_search": {}})
    if resp.status_code == 400 and "google_search" in resp.text:
        # older models (1.5-flash) want the retrieval shape
        resp = await _call({"google_search_retrieval": {}})
    if resp.status_code != 200:
        raise EngineError(f"Gemini grounding {resp.status_code}: {resp.text[:200]}")
    data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise EngineError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise EngineError("Gemini returned an empty answer")

    grounding = candidates[0].get("groundingMetadata") or {}
    cited = [
        str(chunk["web"]["uri"])
        for chunk in (grounding.get("groundingChunks") or [])
        if isinstance(chunk, dict) and isinstance(chunk.get("web"), dict) and chunk["web"].get("uri")
    ]
    queries = [str(q) for q in (grounding.get("webSearchQueries") or []) if q]
    return EngineAnswer(
        engine="gemini",
        prompt_text=prompt_text,
        answer_text=text,
        cited_urls=cited,
        search_queries=queries,
        raw=data,
    )


# --------------------------------------------------------------------------
# FREE TIER (STUBS): logged-out web UIs via browser automation
# --------------------------------------------------------------------------
# No API key buys you ChatGPT's consumer answers. The free lane is driving
# chatgpt.com and perplexity.ai in a cloud browser: paste the prompt, read the
# answer, scrape the cited source links. The functions below define the exact
# contract; implement the bodies with the browser session tooling when the
# ChatGPT/Perplexity lanes matter. run_engines() treats their EngineError as a
# skipped engine, so adding them to the engine list today is safe.

async def run_chatgpt_web(prompt_text: str, **_: Any) -> EngineAnswer:
    """STUB: browser-driven ChatGPT web run.

    Implementation notes for when this gets built:
      1. Open https://chatgpt.com in a cloud browser session (logged out is
         fine; a free logged-in session gives search-enabled answers).
      2. Submit prompt_text in the composer, wait for the answer to finish.
      3. Extract answer text + any linked sources (Search/Deep Research
         answers expose citation chips; read their hrefs).
      4. Return EngineAnswer(engine="chatgpt_web", answer_text=...,
         cited_urls=[...]). Respect rate limits: 10 prompts per cycle is
         nothing, but space requests and stop on any challenge page.
    """
    raise EngineError("chatgpt_web runner not implemented yet (browser stub)")


async def run_perplexity_web(prompt_text: str, **_: Any) -> EngineAnswer:
    """STUB: browser-driven Perplexity web run. Same contract as chatgpt_web;
    perplexity.ai shows numbered source cards whose hrefs are the cited_urls.
    """
    raise EngineError("perplexity_web runner not implemented yet (browser stub)")


# --------------------------------------------------------------------------
# PAID UPGRADES (optional; skipped silently when keys are absent)
# --------------------------------------------------------------------------

async def run_perplexity(prompt_text: str, *, timeout: float = 60.0) -> EngineAnswer:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        raise EngineError("PERPLEXITY_API_KEY is not configured")
    model = os.environ.get("PERPLEXITY_MODEL", "sonar")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Answer as a helpful buying advisor. Recommend specific products with reasons."},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(PERPLEXITY_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    if resp.status_code != 200:
        raise EngineError(f"Perplexity {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content", "") if choices else "") or ""
    cited = [str(u) for u in (data.get("citations") or []) if u]
    if not text:
        raise EngineError("Perplexity returned an empty answer")
    return EngineAnswer(engine="perplexity", prompt_text=prompt_text, answer_text=text.strip(), cited_urls=cited, raw=data)


async def run_openai(prompt_text: str, *, timeout: float = 90.0) -> EngineAnswer:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise EngineError("OPENAI_API_KEY is not configured")
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    payload = {"model": model, "tools": [{"type": "web_search_preview"}], "input": prompt_text}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(OPENAI_RESPONSES_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    if resp.status_code != 200:
        raise EngineError(f"OpenAI {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text_parts: list[str] = []
    cited: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text":
                text_parts.append(block.get("text", ""))
                for ann in block.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        cited.append(str(ann["url"]))
    text = "\n".join(p for p in text_parts if p).strip()
    if not text:
        raise EngineError("OpenAI returned an empty answer")
    seen: set[str] = set()
    cited = [u for u in cited if not (u in seen or seen.add(u))]
    return EngineAnswer(engine="openai", prompt_text=prompt_text, answer_text=text, cited_urls=cited, raw=data)


ENGINE_RUNNERS = {
    # free
    "gemini": run_gemini_grounded,
    "chatgpt_web": run_chatgpt_web,        # stub
    "perplexity_web": run_perplexity_web,  # stub
    # paid upgrades, auto-skipped without keys
    "perplexity": run_perplexity,
    "openai": run_openai,
}

DEFAULT_ENGINES = ("gemini",)  # zero-budget default; add others as they become available


async def run_engines(prompt_text: str, *, engines: tuple[str, ...] = DEFAULT_ENGINES) -> list[EngineAnswer]:
    async def _one(name: str) -> EngineAnswer:
        runner = ENGINE_RUNNERS.get(name)
        if runner is None:
            return EngineAnswer(engine=name, prompt_text=prompt_text, answer_text="", error=f"unknown engine {name}")
        try:
            return await runner(prompt_text)
        except EngineError as exc:
            return EngineAnswer(engine=name, prompt_text=prompt_text, answer_text="", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return EngineAnswer(engine=name, prompt_text=prompt_text, answer_text="", error=f"{type(exc).__name__}: {exc}")

    return list(await asyncio.gather(*(_one(e) for e in engines)))
