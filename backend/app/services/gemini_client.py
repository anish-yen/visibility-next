from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import get_settings


class GeminiError(RuntimeError):
    """Raised when Gemini returns an unusable response."""


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiError("Gemini returned no candidates")

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )
    text_chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
    text = "\n".join(chunk for chunk in text_chunks if chunk).strip()
    if not text:
        raise GeminiError("Gemini returned an empty response")
    return text


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


class GeminiClient:
    def __init__(self) -> None:
        settings = get_settings()
        keys = getattr(settings, "gemini_api_keys", None) or []
        if not keys and settings.gemini_api_key:
            keys = [settings.gemini_api_key]
        self.api_keys = list(keys)
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self.model = settings.gemini_model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_keys:
            raise GeminiError("GEMINI_API_KEY is not configured")

        url = f"{self.base_url}/{self.model}:generateContent"
        last_exc: GeminiError | None = None
        for index, key in enumerate(self.api_keys):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        url,
                        params={"key": key},
                        json=payload,
                    )
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                detail = f"HTTP {status}" if status is not None else type(exc).__name__
                last_exc = GeminiError(f"Gemini request failed: {detail}")
                if status == 429 and index < len(self.api_keys) - 1:
                    print("GEMINI key rate-limited, rotating to next configured key", flush=True)
                    continue
                raise last_exc from exc

            try:
                return response.json()
            except ValueError as exc:
                raise GeminiError("Gemini returned invalid JSON payload") from exc

        if last_exc is not None:
            raise last_exc
        raise GeminiError("Gemini request failed")

    async def generate_json(
        self,
        *,
        system_instruction: str,
        user_prompt: str,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        data = await self._post(payload)
        text = _strip_json_fence(_extract_text(data))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiError("Gemini returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise GeminiError("Gemini JSON response must be an object")
        return parsed

    async def generate_text(
        self,
        *,
        system_instruction: str,
        user_prompt: str,
        temperature: float = 0.4,
    ) -> str:
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        data = await self._post(payload)
        return _extract_text(data)
