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


@app.get("/api/me")
def me(request: Request) -> dict:
    """Example protected route; requires Authorization: Bearer <supabase_access_token>."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "email": getattr(request.state, "user", {}).get("email"),
    }
