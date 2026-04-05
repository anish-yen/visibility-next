# AI Search Visibility Auditor

Monorepo scaffold: **Next.js 14** frontend with Supabase Auth, **FastAPI** backend that validates sessions via Supabase Auth’s `GET /auth/v1/user` API, aligned with `CLAUDE.md`.

## Prerequisites

- Node.js 18+ and npm
- Python 3.10+
- A [Supabase](https://supabase.com) project

## Environment variables

1. Copy `.env.example` to **`frontend/.env.local`** and **`backend/.env`**.
2. Fill in Supabase **Project URL**, **anon** and **service_role** keys from **Settings → API**.
3. Set **`NEXT_PUBLIC_SUPABASE_URL`** and **`NEXT_PUBLIC_SUPABASE_ANON_KEY`** to the same values as **`SUPABASE_URL`** and **`SUPABASE_ANON_KEY`** so the browser client can talk to Supabase.
4. The FastAPI **`SupabaseJWTAuthMiddleware`** checks each request by calling **`GET {SUPABASE_URL}/auth/v1/user`** with **`Authorization: Bearer <access_token>`** (no local JWT secret).
5. Set **`NEXT_PUBLIC_API_URL`** to your FastAPI origin (e.g. `http://localhost:8000`) so the signed-in dashboard can call **`POST /audits`**, **`GET /audits`**, **`GET /audits/{id}`**, and **`POST /audits/{id}/recommendations/{rec_id}/brief`** with the Supabase access token.
6. In Supabase **Authentication → URL Configuration**, add your site URL (e.g. `http://localhost:3000`) and redirect URLs: `http://localhost:3000/auth/callback`, `http://localhost:3000/update-password` (or your deployed equivalents).

Set **`GEMINI_API_KEY`** in **`backend/.env`** for the audit pipeline (prompt generation, simulated visibility, content briefs) using the **`google-genai`** SDK. **`REDIS_URL`** and **`DATABASE_URL`** are reserved for future Celery/Postgres persistence.

Sanity: **`GET http://localhost:8000/test-gemini`** (no auth) after the backend is running.

## Run the frontend (Next.js)

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Use **Sign up**, **Log in**, **Forgot password** (reset email → **Update password** after following the link). With the backend running, the home dashboard runs a multi-step **audit** flow (create → progress → results with charts and briefs).

## Run the backend (FastAPI)

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- **Health (no auth):** [http://localhost:8000/health](http://localhost:8000/health)
- **OpenAPI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Protected example:** `GET http://localhost:8000/api/me` with header `Authorization: Bearer <supabase_access_token>`. Obtain the access token from the Supabase session (e.g. browser devtools / `supabase.auth.getSession()` after login).

All routes except `/health`, `/docs`, `/redoc`, `/openapi.json`, and `OPTIONS` require a Supabase access token accepted by **`GET /auth/v1/user`**.

## Project layout

| Path        | Role                                                |
| ----------- | --------------------------------------------------- |
| `frontend/` | Next.js 14 App Router, Tailwind, Supabase SSR/auth |
| `backend/`  | FastAPI app, Supabase admin client, Auth API token middleware  |

## Backend Supabase client

`get_supabase_admin()` in `backend/app/supabase_client.py` uses the **service role** key for server-side operations that bypass RLS. Use only on the server; never expose that key to the browser.
