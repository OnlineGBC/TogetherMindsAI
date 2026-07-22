"""
tests/conftest.py
-----------------
Session-wide patches applied to every test module:

1. Emotion classifier  — prevents real model download/inference during tests.
2. Claude client       — prevents real API calls during tests.
3. live_server_url     — starts a real Flask/SocketIO server for browser tests.
"""
import os
import socket
import threading
import time

import pytest
from unittest.mock import MagicMock, patch

# The app now fails closed if SECRET_KEY is unset (no hardcoded fallback), so
# guarantee every test module that imports the app has one. Individual test
# files may still set their own; setdefault never overrides those.
os.environ.setdefault("SECRET_KEY", "conftest-test-secret-key")

# ---------------------------------------------------------------------------
# Rate limiter reset — runs before every test to prevent counter bleed-over
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset flask-limiter's in-memory counters before each test.

    Without this, rate-limit tests that exhaust the counter would cause
    subsequent tests in the same process to receive unexpected 429 responses.
    """
    from TogetherMindsAI import limiter
    limiter.reset()
    yield


# ---------------------------------------------------------------------------
# Claude client mock — returned for every test automatically
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_claude_client():
    """Replace the Anthropic client with a lightweight mock.

    Default reply: a generic therapeutic response that satisfies the smoke-test
    assertions (contains "therapist" and passes the output sanitiser).
    Individual tests that need a specific reply should patch on top.
    """
    default_reply = (
        "I hear you, and I'm glad you reached out. "
        "It takes courage to share how you're feeling. "
        "As your AI therapist, I'd like to understand more about what's on your mind. "
        "What feels most pressing for you right now?"
    )
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=default_reply)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# Live server — used by browser (Playwright) tests
# ---------------------------------------------------------------------------

_LIVE_SERVER_PORT = 5099


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    """Block until the given port accepts connections or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Live server did not start on port {port} within {timeout}s")


@pytest.fixture(scope="session")
def live_server_url():
    """Start a real Flask/SocketIO server for Playwright browser tests.

    Runs once per test session. Uses in-memory SQLite with StaticPool so the
    DB is isolated from the developer's local database file.
    The emotion pipeline and Claude client are patched at session scope so the
    server thread always sees the mocks regardless of test function boundaries.
    """
    os.environ["TESTING"] = "1"
    os.environ.setdefault("SECRET_KEY", "browser-test-secret-key-abc123")
    os.environ.setdefault("CORS_ALLOWED_ORIGINS", f"http://127.0.0.1:{_LIVE_SERVER_PORT}")

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from TogetherMindsAI import app, socketio
    from models import db

    # test_auth.py sets CORS_ALLOWED_ORIGINS='http://localhost:5001' before importing
    # the app, which locks that restrictive value into the shared socketio instance.
    # The live server must accept connections from http://127.0.0.1:<port>, so override
    # the engineio CORS setting directly to allow all origins.
    socketio.server.eio.cors_allowed_origins = "*"

    # Flask-SQLAlchemy 3.x caches the engine — changing app.config after init_app()
    # has no effect. Override the cached engine directly so the live-server DB is
    # isolated in memory and never touches the production file.
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}

    with app.app_context():
        db.create_all()

    # Apply mocks at session scope so the server thread always sees them
    default_reply = (
        "I hear you. As your AI therapist, I'm here to support you. "
        "What feels most important to explore today?"
    )
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=default_reply)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client):

        def _run():
            socketio.run(
                app, host="127.0.0.1", port=_LIVE_SERVER_PORT,
                use_reloader=False, allow_unsafe_werkzeug=True,
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        _wait_for_port(_LIVE_SERVER_PORT)

        yield f"http://127.0.0.1:{_LIVE_SERVER_PORT}"
