# Backend image. Pins the interpreter, which is the point: nothing in this
# repo pins Python for Render, so the langgraph / psycopg wheels currently
# resolve against whatever interpreter that service happens to have. 3.14 is
# the version those pins were verified against.
FROM python:3.14-slim

# PYTHONDONTWRITEBYTECODE: no .pyc in the image.
# PYTHONUNBUFFERED: logs reach Render's log stream immediately, which matters
# because the Gemini error-body logging is print-based.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# No apt packages needed: psycopg[binary] bundles its own libpq, which is why
# requirements.txt pins the [binary] extra rather than plain psycopg.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# .dockerignore keeps .env, .venv and caches out of the build context, so no
# credential can be baked into a layer.
COPY . .

# Drop privileges: the crawler fetches attacker-influenced URLs, so the
# process should not be root even with the SSRF guard in front of it.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)"

# Shell form so ${PORT} expands: Render injects the port it wants.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
