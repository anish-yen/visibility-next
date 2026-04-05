from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.middleware import SupabaseJWTAuthMiddleware
from app.routers import audits

app = FastAPI(
    title="AI Search Visibility Auditor API",
    description="Backend for crawl, prompts, and visibility evaluation.",
    version="0.1.0",
)

app.add_middleware(SupabaseJWTAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audits.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/test-crawl")
async def test_crawl(domain: str) -> dict:
    """Temporary MVP check for crawl_site (no auth). Remove before production."""
    from app.services.crawler import crawl_site

    pages = await crawl_site(domain)
    return {"domain": domain, "count": len(pages), "pages": pages}


@app.get("/test-gemini")
async def test_gemini() -> dict:
    """Sanity check for GEMINI_API_KEY and google-genai (no auth)."""
    from google import genai
    from google.genai import types

    from app.config import get_settings

    s = get_settings()
    if not s.gemini_api_key:
        return {"ok": False, "error": "GEMINI_API_KEY is missing or empty in backend/.env"}
    try:
        client = genai.Client(api_key=s.gemini_api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Say hello in one sentence.",
            config=types.GenerateContentConfig(temperature=0.5),
        )
        text = getattr(resp, "text", None) or ""
        if not text and getattr(resp, "candidates", None):
            c0 = resp.candidates[0]
            parts = getattr(getattr(c0, "content", None), "parts", None) or []
            text = "".join(getattr(p, "text", "") or "" for p in parts)
        return {"ok": True, "text": text.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/me")
def me(request: Request) -> dict:
    """Example protected route; requires Authorization: Bearer <supabase_access_token>."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "email": getattr(request.state, "user", {}).get("email"),
    }
