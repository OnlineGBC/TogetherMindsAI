FROM python:3.10-slim

# System dependencies for psycopg2-binary and Pillow (used by fpdf2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Pre-download the emotion classifier so the first request is not slow
# The model is baked into the image; it does NOT need network at runtime.
RUN python -c "\
from transformers import pipeline; \
pipeline('text-classification', \
         model='j-hartmann/emotion-english-distilroberta-base', \
         top_k=1, device=-1, truncation=True, max_length=512)"

# Copy application code
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
