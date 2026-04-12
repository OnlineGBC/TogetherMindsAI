FROM python:3.10-slim

# System dependencies for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Step 1: install torch CPU-only FIRST (before requirements.txt) ──────────
# The CPU wheel is ~200 MB vs ~2.5 GB for the default CUDA build on PyPI.
# Installing this first lets Docker cache it separately from app dependencies.
# typing-extensions must be pre-installed from PyPI before torch because the
# PyTorch index has a naming inconsistency that prevents pip resolving it there.
RUN pip install --no-cache-dir "typing-extensions>=4.15.0"
RUN pip install --no-cache-dir \
    torch==2.6.0 \
    --index-url https://download.pytorch.org/whl/cpu

# ── Step 2: install everything else (torch is already satisfied above) ───────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Step 3: pre-download the emotion classifier into the image ───────────────
# Baking the model in means zero download time at runtime — even on cold starts.
# HF_TOKEN is passed as a build arg so the download is authenticated.
ARG HF_TOKEN
ENV HF_TOKEN=$HF_TOKEN
# Disable Xet (new HF transfer protocol) — falls back to standard HTTP download,
# which is more reliable in restricted network environments like Cloud Build.
ENV HF_HUB_DISABLE_XET=1
RUN python -c "\
from transformers import pipeline; \
pipeline('text-classification', \
         model='j-hartmann/emotion-english-distilroberta-base', \
         top_k=1, device=-1, truncation=True, max_length=512)"

# ── Step 4: copy application code ───────────────────────────────────────────
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
