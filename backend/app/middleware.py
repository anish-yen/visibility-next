from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
import httpx

from app.config import get_settings


def _is_public_path(path: str) -> bool:
    if path in ("/health", "/openapi.json"):
        return True
    if path.startswith("/docs") or path.startswith("/redoc"):
        return True
    if path == "/test-crawl" or path == "/test-gemini":
        return True
    return False


class SupabaseJWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates the Bearer token by calling Supabase Auth GET /auth/v1/user.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if request.method == "OPTIONS" or _is_public_path(path):
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if not auth or not auth.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth[7:].strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing bearer token"},
            )

        settings = get_settings()
        base = settings.supabase_url.rstrip("/")
        if not base:
            return JSONResponse(
                status_code=500,
                content={"detail": "SUPABASE_URL is not configured"},
            )
        if not settings.supabase_anon_key:
            return JSONResponse(
                status_code=500,
                content={"detail": "SUPABASE_ANON_KEY is not configured"},
            )

        url = f"{base}/auth/v1/user"
        headers = {
            "Authorization": f"Bearer {token}",
            "apikey": settings.supabase_anon_key,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=15.0)
        except httpx.RequestError:
            return JSONResponse(
                status_code=503,
                content={"detail": "Could not reach Supabase Auth"},
            )

        if response.status_code != 200:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        try:
            user = response.json()
        except ValueError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid auth response"},
            )

        user_id = user.get("id")
        if not user_id:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid auth response"},
            )

        request.state.user = user
        request.state.user_id = user_id

        return await call_next(request)
