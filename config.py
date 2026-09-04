"""
config.py
---------
Central configuration module.

Reads environment variables once at import time and exposes typed constants.
Works identically on:
  - Local laptop / Android / iPhone  (SQLite, .env file)
  - Google Cloud Run                  (PostgreSQL, secrets injected by Secret Manager)

Nothing in this module writes to the database or imports Flask — it is safe to
import before the app is created.
"""

import os
import subprocess
import sys

from dotenv import load_dotenv

# load_dotenv() is a no-op when the variables are already set in the environment
# (e.g. Cloud Run injects secrets directly) so it is always safe to call.
load_dotenv()

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

# True when running under pytest
IS_TESTING: bool = os.environ.get("TESTING", "false").lower() in ("1", "true")

# True when the DATABASE_URL points at PostgreSQL (Cloud Run)
_db_url: str = os.environ.get("DATABASE_URL", "sqlite:///togethermindsai.db")
IS_SQLITE: bool = _db_url.startswith("sqlite")
IS_PRODUCTION: bool = not IS_SQLITE and not IS_TESTING

# ---------------------------------------------------------------------------
# Flask core
# ---------------------------------------------------------------------------

SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
FLASK_DEBUG: bool = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

# Auto-logoff: a logged-in session is invalidated after this many seconds of
# inactivity (HIPAA § 164.312(a)(2)(iii) automatic logoff). Each request from a
# logged-in user refreshes the clock; the live-session heartbeat counts as
# activity, so an open session console never times out mid-session.
IDLE_TIMEOUT_SECONDS: int = int(os.environ.get("IDLE_TIMEOUT_SECONDS", str(30 * 60)))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL: str = _db_url

# SQLAlchemy engine kwargs differ between SQLite and PostgreSQL.
# StaticPool is intentionally NOT used here — it belongs only in test fixtures
# where an in-memory SQLite DB must share a single connection across threads.
# File-based SQLite and PostgreSQL both use their default connection pools.
if IS_SQLITE:
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "connect_args": {"check_same_thread": False},
    }
else:
    # PostgreSQL on Cloud SQL — use a modest connection pool
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_size": 5,
        "max_overflow": 10,
        "pool_pre_ping": True,
    }

# ---------------------------------------------------------------------------
# SocketIO / async
# ---------------------------------------------------------------------------

_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5001").strip()
# Flask-SocketIO requires the string "*" (not the list ["*"]) to allow all origins.
CORS_ALLOWED_ORIGINS: "str | list[str]" = (
    "*" if _cors_raw == "*" else [o.strip() for o in _cors_raw.split(",")]
)

# threading for local dev / tests (supports hot-reload on Windows);
# eventlet for production async performance
ASYNC_MODE: str = "threading" if (FLASK_DEBUG or IS_TESTING) else "eventlet"

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

RATE_WINDOW_SECONDS: int = int(os.environ.get("RATE_WINDOW_SECONDS", "60"))
RATE_MAX_MESSAGES: int = int(os.environ.get("RATE_MAX_MESSAGES", "20"))
MAX_MESSAGE_LENGTH: int = int(os.environ.get("MAX_MESSAGE_LENGTH", "8000"))

# Seconds the AI waits before responding again in couple/group mode.
# Prevents the AI from interrupting mid-exchange when partners send rapidly.
AI_COOLDOWN_SECONDS: int = int(os.environ.get("AI_COOLDOWN_SECONDS", "20"))

# Seconds of total silence (no user or AI messages) after which the AI sends
# a brief re-engagement nudge in couple/group sessions. Set to 0 to disable.
SILENCE_NUDGE_SECONDS: int = int(os.environ.get("SILENCE_NUDGE_SECONDS", "45"))

# ---------------------------------------------------------------------------
# Field-level encryption
# ---------------------------------------------------------------------------

# Fernet key (locally) or KMS URI (Cloud Run).
# WARNING: never rotate this key without first re-encrypting all existing
# ChatMessage rows — otherwise all stored messages become unreadable.
FIELD_ENCRYPTION_KEY: str = os.environ.get("FIELD_ENCRYPTION_KEY", "")

# ---------------------------------------------------------------------------
# Startup validation — called once from TogetherMindsAI.py
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Clinical-reference semantic retrieval (local embeddings)
# ---------------------------------------------------------------------------
# The co-pilot's ICD grounding matches the transcript against a curated corpus.
# When enabled, matching is done by MEANING via a small local embedding model
# (fastembed / ONNX, in-process — no transcript text ever leaves the instance),
# so everyday phrasing the keyword list misses still grounds. It degrades to
# pure keyword matching whenever the model is unavailable. No API key, no secret
# — the model is baked into the Docker image. clinical_reference.py reads these
# same env vars directly; they are surfaced here for discoverability.
EMBEDDING_ENABLED: bool = os.environ.get("EMBEDDING_ENABLED", "true").lower() in ("1", "true", "yes")
EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# Cosine-similarity thresholds for ICD grounding — tunable without a code change
# (env locally, GSM secret on Cloud Run). Card threshold is stricter than the
# prompt-block one. clinical_reference.py reads these same vars directly.
EMBEDDING_MIN_SIM_CARD: float = float(os.environ.get("EMBEDDING_MIN_SIM_CARD", "0.58"))
EMBEDDING_MIN_SIM_BLOCK: float = float(os.environ.get("EMBEDDING_MIN_SIM_BLOCK", "0.55"))

# ---------------------------------------------------------------------------
# Clinician OAuth login (OpenID Connect) — Google & Microsoft.
# Optional: the app starts fine without these, but the /login buttons only work
# once the corresponding client id/secret are set. No email/PII is stored — only
# the provider's opaque subject id. Keep secrets in .env (local) / Secret Manager.
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID: str        = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str    = os.environ.get("GOOGLE_CLIENT_SECRET", "")
MICROSOFT_CLIENT_ID: str     = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET: str = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
# "common" lets any work/school or personal Microsoft account sign in.
MICROSOFT_TENANT: str        = os.environ.get("MICROSOFT_TENANT", "common")

# ---------------------------------------------------------------------------
# Realtime conferencing (Phase 1) — LiveKit audio + AssemblyAI streaming STT.
# Optional: the app runs fine without these; the in-session audio/transcription
# only activates when all are set. Keep secrets in .env (local) / Secret Manager.
# ---------------------------------------------------------------------------

LIVEKIT_URL: str        = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY: str    = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET: str = os.environ.get("LIVEKIT_API_SECRET", "")
ASSEMBLYAI_API_KEY: str = os.environ.get("ASSEMBLYAI_API_KEY", "")

# True when both the audio (LiveKit) and STT (AssemblyAI) backends are configured.
RTC_ENABLED: bool = bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET and ASSEMBLYAI_API_KEY)

# ---------------------------------------------------------------------------
# Session recording (Phase 4 — optional paid A/V recording). OFF by default.
# When enabled, recordings are made by the self-hosted LiveKit Egress service and
# uploaded to the private recordings bucket.
# ---------------------------------------------------------------------------

RECORDING_ENABLED: bool = os.environ.get("RECORDING_ENABLED", "false").lower() in ("1", "true", "yes")
RECORDINGS_BUCKET: str = os.environ.get("RECORDINGS_BUCKET", "togethermindsai-recordings")
# Public origin used to build absolute links in emails sent outside a request
# context (e.g. the recording download link in retention notices).
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", "https://tm.onlinegbc.com").rstrip("/")

# ---------------------------------------------------------------------------
# Subscription billing (Phase 4 Step 4 — Stripe). Three clinician tiers:
#   free          — reflections chat + transcript
#   pro     ($10) — + AI analysis (therapist co-pilot + session summary)
#   premium ($25) — + audio/video recording
# OFF by default. While OFF every clinician keeps full access (entitlement checks
# short-circuit to True), so production is unchanged until billing is switched on
# with the Stripe price IDs configured. Keep all secrets in .env / Secret Manager.
# ---------------------------------------------------------------------------

BILLING_ENABLED: bool       = os.environ.get("BILLING_ENABLED", "false").lower() in ("1", "true", "yes")
STRIPE_SECRET_KEY: str      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
# Stripe Price IDs. Price now follows the ROLE, not a feature tier: one paid
# plan per role, and paying unlocks everything that role offers.
#   clinical  $16/mo  — psychotherapists and hypnotherapists/coaches
#   caregiver $9.99/mo — recording, 40 hours a month
#   top-up    $9.99 ONE TIME — another 40 recording hours
STRIPE_PRICE_CLINICAL: str    = os.environ.get("STRIPE_PRICE_CLINICAL", "")
STRIPE_PRICE_CAREGIVER: str   = os.environ.get("STRIPE_PRICE_CAREGIVER", "")
STRIPE_PRICE_HOURS_TOPUP: str = os.environ.get("STRIPE_PRICE_HOURS_TOPUP", "")
# Retired Pro/Premium tiers. Kept only so an old stored plan can still be
# recognised and mapped to "free"; nothing new is ever sold at these prices.
STRIPE_PRICE_PRO: str       = os.environ.get("STRIPE_PRICE_PRO", "")
STRIPE_PRICE_PREMIUM: str   = os.environ.get("STRIPE_PRICE_PREMIUM", "")

# ---------------------------------------------------------------------------
# EHR integration — SMART on FHIR launch from inside Epic / Oracle Health.
#
# OFF by default, and the routes 404 while it is off, the same way the admin
# console hides itself. Nothing about this appears in production until it is
# switched on deliberately.
#
# EHR_ALLOWED_ISS is not optional. /ehr/launch is handed a FHIR base URL by the
# EHR and then trusts it — for discovery, and to send an authorization code to.
# Without an allowlist anyone could launch us at a server they control and
# collect our client id and codes. So only these bases are ever accepted.
# Comma-separated; the Epic sandbox is the default.
# ---------------------------------------------------------------------------

EHR_ENABLED: bool = os.environ.get("EHR_ENABLED", "false").lower() in ("1", "true", "yes")

EPIC_CLIENT_ID: str = os.environ.get("EPIC_CLIENT_ID", "")
# Sandbox only. Production confidential clients use asymmetric JWT auth against a
# JWKS we would have to host, which is a later job.
EPIC_SANDBOX_CLIENT_SECRET: str = os.environ.get("EPIC_SANDBOX_CLIENT_SECRET", "")

_default_iss = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
EHR_ALLOWED_ISS: tuple = tuple(
    s.strip().rstrip("/")
    for s in os.environ.get("EHR_ALLOWED_ISS", _default_iss).split(",")
    if s.strip()
)

# What we ask for at launch. Kept here rather than in code so a customer that
# grants less can be accommodated without a deploy.
#   launch      the EHR tells us which patient is open
#   fhirUser    who the clinician is
#   *.read      the two resources phase 1 reads
EHR_SCOPES: str = os.environ.get(
    "EHR_SCOPES", "launch openid fhirUser patient/Patient.read patient/Encounter.read")

# ---------------------------------------------------------------------------
# Feedback form — Gmail SMTP send (no DB storage, no audit log)
# ---------------------------------------------------------------------------

FEEDBACK_SMTP_HOST: str = os.environ.get("FEEDBACK_SMTP_HOST", "smtp.gmail.com")
FEEDBACK_SMTP_PORT: int = int(os.environ.get("FEEDBACK_SMTP_PORT", "587"))
FEEDBACK_SMTP_USER: str = os.environ.get("FEEDBACK_SMTP_USER", "")
FEEDBACK_SMTP_PASSWORD: str = os.environ.get("FEEDBACK_SMTP_PASSWORD", "")
# Comma-separated list of recipients. Stored as a list internally; rendered
# back to a comma-separated string when set on the email To: header.
_feedback_to_raw: str = os.environ.get("FEEDBACK_TO_EMAIL", "raja@onlinegbc.com")
FEEDBACK_TO_EMAILS: list = [addr.strip() for addr in _feedback_to_raw.split(",") if addr.strip()]
# Kept for backward-compat with anything that still imports the old name —
# always the first recipient. New code should use FEEDBACK_TO_EMAILS.
FEEDBACK_TO_EMAIL: str = FEEDBACK_TO_EMAILS[0] if FEEDBACK_TO_EMAILS else ""
FEEDBACK_FROM_EMAIL: str = os.environ.get("FEEDBACK_FROM_EMAIL", FEEDBACK_SMTP_USER)


# ---------------------------------------------------------------------------
# Admin comp-access console (/accessadmin) — grant full access to an email
# without payment. Guarded by two factors, EITHER of which gets you in: an
# authenticator app (TOTP) or a code emailed to the admin. With no ADMIN_EMAILS
# set the console stays unreachable, so an unconfigured deploy cannot expose it.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Recording quality. LiveKit's default room-composite preset is 1080p at roughly
# 3-4 Mbps, which produced ~1 GB for a 45-minute session. These are talking-head
# sessions, so 720p at 1200 kbps is visually equivalent for faces and about 60%
# smaller. Tunable by env without a code change.
# ---------------------------------------------------------------------------

RECORDING_WIDTH: int = int(os.environ.get("RECORDING_WIDTH", "1280"))
RECORDING_HEIGHT: int = int(os.environ.get("RECORDING_HEIGHT", "720"))
RECORDING_FRAMERATE: int = int(os.environ.get("RECORDING_FRAMERATE", "30"))
RECORDING_VIDEO_KBPS: int = int(os.environ.get("RECORDING_VIDEO_KBPS", "1200"))
RECORDING_AUDIO_KBPS: int = int(os.environ.get("RECORDING_AUDIO_KBPS", "96"))

# Roll a recording over to a new file at this age, bounding any single file's
# size. The egress writes on the LiveKit VM and only uploads when it stops, so
# there is no file to measure mid-recording — but with the bitrate pinned above,
# size is arithmetic: ~9.7 MB/min, so 90 minutes is roughly 875 MB.
RECORDING_MAX_MINUTES: int = int(os.environ.get("RECORDING_MAX_MINUTES", "90"))
# How often to check for recordings due to roll over. The cap is effectively
# RECORDING_MAX_MINUTES + this interval.
RECORDING_ROLLOVER_CHECK_MINUTES: int = int(
    os.environ.get("RECORDING_ROLLOVER_CHECK_MINUTES", "5"))

# Joining rolled-over segments back into one file at the end of a session.
FFMPEG_BIN: str = os.environ.get("FFMPEG_BIN", "ffmpeg")
# Signed URLs ffmpeg reads the segments through must outlive the whole join.
RECORDING_CONCAT_URL_MINUTES: int = int(
    os.environ.get("RECORDING_CONCAT_URL_MINUTES", "120"))
RECORDING_CONCAT_TIMEOUT_SECONDS: int = int(
    os.environ.get("RECORDING_CONCAT_TIMEOUT_SECONDS", "1800"))


_admin_emails_raw: str = os.environ.get("ADMIN_EMAILS", "")
ADMIN_EMAILS: list = [a.strip().lower() for a in _admin_emails_raw.split(",") if a.strip()]
ADMIN_TOTP_SECRET: str = os.environ.get("ADMIN_TOTP_SECRET", "")

# How long an admin stays verified before being challenged again, and how long a
# one-time code stays usable.
ADMIN_SESSION_MINUTES: int = int(os.environ.get("ADMIN_SESSION_MINUTES", "15"))
ADMIN_CODE_TTL_MINUTES: int = int(os.environ.get("ADMIN_CODE_TTL_MINUTES", "10"))
ADMIN_CODE_MAX_ATTEMPTS: int = int(os.environ.get("ADMIN_CODE_MAX_ATTEMPTS", "5"))
# Either factor on its own is enough (the admin has already signed in with OAuth
# as a configured admin address, so this is the second factor, not the first).
ADMIN_FACTORS_REQUIRED: int = int(os.environ.get("ADMIN_FACTORS_REQUIRED", "1"))

ADMIN_CONSOLE_ENABLED: bool = bool(ADMIN_EMAILS)

# Discount-code usage alerts. A sweep asks Stripe how many times each code has
# been redeemed and emails the partner and every admin when a code is nearly
# spent, fully spent, or being used unusually fast — the last being what a leaked
# code looks like. Settings rather than constants so a noisy threshold can be
# retuned without a deploy.
PROMO_ALERT_SWEEP_MINUTES: int = int(os.environ.get("PROMO_ALERT_SWEEP_MINUTES", "60"))
PROMO_ALERT_NEARLY_SPENT_PCT: int = int(os.environ.get("PROMO_ALERT_NEARLY_SPENT_PCT", "80"))
PROMO_ALERT_BURST_PER_SWEEP: int = int(os.environ.get("PROMO_ALERT_BURST_PER_SWEEP", "5"))


def secure_env_file() -> None:
    """Restrict .env file permissions to the current OS user on every startup.

    On Windows uses icacls; on Unix/Mac uses chmod 600.
    Skips silently if .env does not exist (e.g. Cloud Run where secrets are
    injected from Secret Manager and no .env file is present).
    Logs a warning if the permission command fails but does not raise —
    a permission failure should not prevent the app from starting.
    """
    import logging
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return

    try:
        if sys.platform == "win32":
            username = os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", env_path, "/inheritance:r", "/grant:r", f"{username}:R"],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(["chmod", "600", env_path], check=True, capture_output=True)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Could not restrict .env file permissions: %s", exc
        )


def validate_config() -> None:
    """Raise RuntimeError listing every missing required variable.

    Uses module-level constants (captured at import/reload time) so it is
    testable via importlib.reload without keeping os.environ patched at
    call time.

    Called at app startup (skipped during tests to keep fixture setup simple).
    """
    if IS_TESTING:
        return

    missing = []
    if not SECRET_KEY:
        missing.append("SECRET_KEY")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not FIELD_ENCRYPTION_KEY:
        missing.append("FIELD_ENCRYPTION_KEY")

    if IS_PRODUCTION:
        if not DATABASE_URL:
            missing.append("DATABASE_URL")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nSee .env.example for documentation."
        )

    if not IS_SQLITE and "postgresql" not in DATABASE_URL and "postgres" not in DATABASE_URL:
        raise RuntimeError(
            f"DATABASE_URL does not look like a PostgreSQL URL: {DATABASE_URL!r}\n"
            "On Cloud Run set DATABASE_URL to a postgresql:// connection string."
        )
