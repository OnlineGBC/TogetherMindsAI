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
