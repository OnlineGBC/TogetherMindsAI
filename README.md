# TogetherMindsAI

Non-medical AI **reflections** app for solo, couple, and group sessions. Users join an anonymous session, share what's on their mind, and Claude responds with reflective, non-clinical guidance. Couple and group sessions are real-time multi-party chats where the AI moderates and reflects.

> TogetherMindsAI is a reflections app, not a therapy or medical app. It does not provide diagnosis or treatment.

---

## Features

- **Three session modes** — solo, couple, group (templates: `solo.html`, `couple.html`, `group.html`)
- **Anonymous session codes** — join/rejoin via short codes (no account needed); see `session_id.py` and `SESSION_ID_REFERENCE.md`
- **Real-time multi-party chat** — Flask-SocketIO with eventlet on production, threading on local dev
- **AI moderation** — Anthropic Claude API for reflection responses, silence nudges, and crisis/medical/off-topic guardrails (`ai_therapist.py`)
- **Emotion detection** — HuggingFace `j-hartmann/emotion-english-distilroberta-base` (CPU, baked into the Docker image)
- **Encrypted message storage at rest** — Fernet via `sqlalchemy-utils` `EncryptedType` (`models.py`)
- **PHI-aware logging** — `log_filter.py` redacts sensitive fields from all log output
- **Per-IP rate limiting** — `flask-limiter`, in-memory store
- **Audit log** — `audit.py` records security/safety events
- **Feedback form** — modal + email delivery via SMTP (see `_feedback_modal.html`, `feedback.html`)
- **Risk audit generator** — `generate_risk_audit.py` produces `TogetherMindsAI_Risk_Audit.docx`
- **Android wrapper** — Trusted Web Activity (`twa-manifest.json` + `app/`, `build.gradle`); produces `app-release-signed.apk` / `.aab`

---

## Tech Stack

| Layer | Local | Production (Cloud Run) |
|---|---|---|
| Runtime | Python 3.10 | Python 3.10 (Docker) |
| WSGI | werkzeug dev server (threading) | gunicorn + eventlet (1 worker) |
| Database | SQLite (`togethermindsai.db`) | PostgreSQL (Cloud SQL) |
| Realtime | Flask-SocketIO (no WS upgrade) | Flask-SocketIO over WebSocket |
| AI | Anthropic Claude API | Anthropic Claude API |
| Emotion model | HuggingFace transformers (CPU) | Same, baked into image |
| Secrets | `.env` via `python-dotenv` | GCP Secret Manager |

Key dependencies: `Flask 3.1`, `Flask-SocketIO 5.6`, `Flask-SQLAlchemy 3.1`, `eventlet 0.41`, `anthropic 0.77`, `transformers 4.52`, `cryptography 44`, `sqlalchemy-utils 0.41.2` (pinned — see `requirements.txt`), `apscheduler`, `gunicorn`, `alembic`.

---

## Repository Layout

```
TogetherMindsAI.py        # Flask + SocketIO app entry point
ai_therapist.py           # Claude prompts, guardrails, silence nudge, escalation
audit.py                  # Audit-log helpers
config.py                 # Env-var loader, platform detection (SQLite vs Postgres)
log_filter.py             # PHI redaction filter for logging
models.py                 # SQLAlchemy models, Fernet encryption setup
session_id.py             # Anonymous session-code generation + parsing
generate_risk_audit.py    # Builds Risk Audit DOCX
migrate_encrypt_messages.py
templates/                # Jinja templates (home, solo, couple, group, auth, etc.)
static/                   # Static assets
scripts/                  # simulate_solo_chat.py, simulate_couple_chat.py, simulate_group_chat.py
tests/                    # pytest suite
app/, build.gradle, ...   # Android TWA wrapper
Dockerfile, cloudbuild.yaml
```

---

## Local Development

Assumes the venv (`TogetherMindsAI.venv/`) is already activated.

1. Install dependencies (first time / when `requirements.txt` changes):
   ```powershell
   pip install -r requirements.txt
   pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
   ```
2. **Local secrets are encrypted at rest** with SOPS + Google Cloud KMS — see
   [SECRETS.md](SECRETS.md). One-time setup: install SOPS
   (`winget install --id SecretsOPerationS.SOPS -e`) and authorise it
   (`gcloud auth application-default login`). Secrets live in the encrypted
   `sops.env` vault — there is **no plaintext `.env`**. Minimum keys are
   `SECRET_KEY`, `ANTHROPIC_API_KEY`, `FIELD_ENCRYPTION_KEY` (`DATABASE_URL`
   defaults to local SQLite if unset). Edit a secret with `sops sops.env`.
3. Run the app (secrets are decrypted into memory only, never written to disk):
   ```powershell
   sops exec-env sops.env 'python TogetherMindsAI.py'
   ```
4. Open `http://127.0.0.1:5000`.

### Tests

```powershell
python -m pytest tests/ -v
```

Notable test files: `test_ai_therapist.py`, `test_security.py`, `test_crisis.py`, `test_input_validation.py`, `test_rate_limit.py`, `test_smoke.py`.

The full pytest suite is the pre-deploy gate — run it (green) before every deploy:

```powershell
python -m pytest tests/
```

---

## Deploy (Google Cloud Run)

Deploys are **manual** — pushing to `main` does **not** auto-deploy. Trigger from the Cloud Build UI or:

```powershell
gcloud builds submit --config cloudbuild.yaml .
```

`cloudbuild.yaml` builds the image, pushes to Artifact Registry, and deploys to Cloud Run with:

- `--min-instances 1` (avoids 10–15 s cold-start penalty for SocketIO sessions)
- `--max-instances 1`, `--memory 2Gi`, `--cpu 1`, `--timeout 120`
- Cloud SQL attached via `--add-cloudsql-instances`
- Secrets injected from Secret Manager: `SECRET_KEY`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `CORS_ALLOWED_ORIGINS`, `FIELD_ENCRYPTION_KEY`, `RATE_*`, `MAX_MESSAGE_LENGTH`, `AI_COOLDOWN_SECONDS`, `SILENCE_NUDGE_SECONDS`, `FEEDBACK_SMTP_*`, `FEEDBACK_TO_EMAIL`

> `CORS_ALLOWED_ORIGINS` must include the custom domain (e.g. `https://tm.onlinegbc.com`) or SocketIO will fail in production.

The Dockerfile installs CPU-only PyTorch (≈200 MB vs 2.5 GB CUDA) and pre-downloads the emotion model into the image so cold starts don't pay download time.

---

## Android (TWA)

The Android app is a Trusted Web Activity wrapper around the deployed web app — no native UI code. Build artifacts (`app-release-signed.apk`, `app-release-bundle.aab`) are checked in at the repo root. Configuration lives in `twa-manifest.json`, `build.gradle`, `app/`.

---

## Security & Compliance Notes

- Session cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production
- Automatic logoff after 30 min idle; session regenerated on login (anti session-fixation)
- Field-level encryption (Fernet) on stored chat messages, session summaries, co-pilot cards, **display names, and session labels** (session labels use a deterministic HMAC lookup key so they stay unique/searchable while encrypted)
- Content-Security-Policy **enforced** (per-request nonce on inline scripts; `unsafe-eval` scoped to the live-session page only, for the video background-blur library)
- HSTS in production, plus `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
- Per-IP rate limits on chat endpoints
- 30-day data retention enforced by `apscheduler`
- PHI redaction in logs (covers `text` / `content` / `message` / `transcript` / `body` / `prompt` / `response`)
- Local-dev secrets encrypted at rest with SOPS + GCP KMS (see [SECRETS.md](SECRETS.md))
- Crisis / medical / off-topic safe responses
- Risk audit: regenerate with `python generate_risk_audit.py`

---

## License

See [LICENSE](LICENSE).
