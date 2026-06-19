FROM python:3.10-slim

# System dependencies: libpq-dev/gcc for psycopg2-binary; libgomp1 for the
# onnxruntime backend used by fastembed (local embedding model).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Step 1: install Python dependencies ──────────────────────────────────────
# REBUILD_DEPS: increment in cloudbuild.yaml whenever requirements.txt changes
# to force Docker to bypass the --cache-from registry cache for this layer.
ARG REBUILD_DEPS=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Step 1b: pre-bake the embedding model into the image ─────────────────────
# Downloads BAAI/bge-small-en-v1.5 at build time into a fixed cache dir so the
# first live request pays no network fetch. Runtime reads the same dir.
ENV FASTEMBED_CACHE_DIR=/app/.fastembed_cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/app/.fastembed_cache')"

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
