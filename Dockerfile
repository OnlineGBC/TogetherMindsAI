FROM python:3.10-slim

# System dependencies for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Step 1: install Python dependencies ──────────────────────────────────────
# REBUILD_DEPS: increment in cloudbuild.yaml whenever requirements.txt changes
# to force Docker to bypass the --cache-from registry cache for this layer.
ARG REBUILD_DEPS=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Step 2: copy application code ────────────────────────────────────────────
COPY . .

# Cloud Run injects PORT; default to 8080
ENV PORT=8080

# gunicorn + eventlet — 1 worker is correct for eventlet (it is async internally)
CMD exec gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --worker-class eventlet \
    --workers 1 \
    --threads 1 \
    --timeout 120 \
    "TogetherMindsAI:app"
