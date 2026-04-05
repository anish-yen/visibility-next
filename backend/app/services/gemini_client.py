"""
Thin wrapper around the official Google Gen AI SDK (`google-genai`).
Used for prompt generation, visibility simulation, and content briefs — not live engine scraping.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Stable, cost-effective default; override via GEMINI_MODEL in env if added later
DEFAULT_MODEL = "gemini-2.5-flash"
LITE_MODEL = "gemini-2.5-flash-lite"
_MAX_ATTEMPTS = 3
_BACKOFF_SEC = 1.5


def require_gemini_api_key() -> str:
    key = get_settings().gemini_api_key
    if not key or not str(key).strip():
        raise RuntimeError(
            "GEMINI_API_KEY is missing or empty. Set it in backend/.env to run the audit LLM pipeline.",
        )
    return str(key).strip()


def _extract_text(response: object) -> str:
    t = getattr(response, "text", None)
    if t:
        return str(t).strip()
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return ""
    chunks: list[str] = []
    for p in parts:
        txt = getattr(p, "text", None)
        if txt:
            chunks.append(txt)
    return "".join(chunks).strip()


def generate_text(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.35,
) -> str:
    """
    Run a single generateContent call with system instruction + user text.
    Retries on transient failures; does not swallow invalid API key errors.
    """
    from google import genai
    from google.genai import types as genai_types

    api_key = require_gemini_api_key()
    client = genai.Client(api_key=api_key)

    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                ),
            )
            text = _extract_text(response)
            if text:
                return text
            last_error = RuntimeError("Empty response text from Gemini")
        except Exception as e:
            last_error = e
            logger.warning(
                "Gemini generate_content attempt %s/%s failed: %s",
                attempt + 1,
                _MAX_ATTEMPTS,
                e,
            )
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_SEC * (attempt + 1))

    # Fallback: no system_instruction (some API surfaces reject certain config combos)
    logger.info("Retrying Gemini without system_instruction merge")
    merged = f"{system_prompt.strip()}\n\n---\n\n{user_prompt.strip()}"
    try:
        response = client.models.generate_content(
            model=model,
            contents=merged,
            config=genai_types.GenerateContentConfig(temperature=temperature),
        )
        text = _extract_text(response)
        if text:
            return text
    except Exception as e:
        last_error = e

    raise RuntimeError(f"Gemini request failed after retries: {last_error}")
