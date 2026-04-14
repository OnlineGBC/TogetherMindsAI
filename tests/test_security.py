"""
tests/test_security.py
----------------------
Tests covering security hardening:
  - secure_env_file() runs without error on the current OS
  - ChatMessage.text is stored encrypted (raw DB value is not readable plaintext)
  - validate_config() raises when FIELD_ENCRYPTION_KEY is missing
  - 30-day purge job deletes expired sessions and their messages
"""

import os
import sys
import importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-security")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from TogetherMindsAI import app, _purge_expired_sessions
from models import db, ChatMessage, TherapySession, init_encryption
from session_id import generate_session_id

# Initialise encryption with the test key once at import time
init_encryption(TEST_KEY)


# ---------------------------------------------------------------------------
# Fixture — isolated in-memory DB per test, matching test_smoke.py pattern
# ---------------------------------------------------------------------------

@pytest.fixture
def enc_client():
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# secure_env_file
# ---------------------------------------------------------------------------

class TestSecureEnvFile:
    def test_runs_without_error_when_env_missing(self, tmp_path):
        """Should skip silently when .env does not exist."""
        import config as cfg
        with patch("os.path.dirname", return_value=str(tmp_path)):
            cfg.secure_env_file()  # must not raise

    def test_runs_without_error_on_current_os(self, tmp_path):
        """Should apply permissions to a real .env file without raising."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST=1")
        import config as cfg
        with patch("os.path.dirname", return_value=str(tmp_path)):
            cfg.secure_env_file()  # must not raise


# ---------------------------------------------------------------------------
# Field encryption
# ---------------------------------------------------------------------------

class TestFieldEncryption:
    def test_stored_text_is_not_plaintext(self, enc_client):
        """Raw DB value of ChatMessage.text must not equal the original message."""
        original_text = "This is a sensitive therapy message."

        with app.app_context():
            msg = ChatMessage(
                session_id=generate_session_id(),
                user_id="test-user",
                text=original_text,
            )
            db.session.add(msg)
            db.session.commit()
            msg_id = msg.id

            # Read raw value directly bypassing ORM decryption
            from sqlalchemy import text as sql_text
            raw = db.session.execute(
                sql_text("SELECT text FROM chat_messages WHERE id = :id"),
                {"id": msg_id},
            ).scalar()

            assert str(raw) != original_text, "Raw DB value should be encrypted, not plaintext"
            assert original_text not in str(raw), "Plaintext must not appear in stored value"

            # ORM must decrypt back to the original
            fetched = db.session.get(ChatMessage, msg_id)
            assert fetched.text == original_text

    def test_decrypted_value_matches_original(self, enc_client):
        """ORM read must return the exact original message after encrypt/decrypt round-trip."""
        original_text = "Decryption round-trip check."

        with app.app_context():
            msg = ChatMessage(
                session_id=generate_session_id(),
                user_id="test-user-2",
                text=original_text,
            )
            db.session.add(msg)
            db.session.commit()

            fetched = db.session.get(ChatMessage, msg.id)
            assert fetched.text == original_text


# ---------------------------------------------------------------------------
# validate_config raises when FIELD_ENCRYPTION_KEY missing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 30-day purge job
# ---------------------------------------------------------------------------

class TestPurgeExpiredSessions:
    def test_expired_session_and_messages_are_deleted(self, enc_client):
        """Sessions past retention_expires_at must be deleted along with their messages."""
        with app.app_context():
            expired_sid = generate_session_id()
            ts = TherapySession(
                id=expired_sid, mode="solo", created_by="purge-test-user",
                created_at=datetime.now(timezone.utc) - timedelta(days=31),
                retention_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
            msg = ChatMessage(session_id=expired_sid, user_id="purge-test-user", text="should be deleted")
            db.session.add(ts)
            db.session.add(msg)
            db.session.commit()

            _purge_expired_sessions()

            assert TherapySession.query.get(expired_sid) is None
            assert ChatMessage.query.filter_by(session_id=expired_sid).count() == 0

    def test_non_expired_session_is_kept(self, enc_client):
        """Sessions within the retention window must not be deleted."""
        with app.app_context():
            active_sid = generate_session_id()
            ts = TherapySession(
                id=active_sid, mode="solo", created_by="keep-test-user",
                created_at=datetime.now(timezone.utc),
                retention_expires_at=datetime.now(timezone.utc) + timedelta(days=29),
            )
            db.session.add(ts)
            db.session.commit()

            _purge_expired_sessions()

            assert TherapySession.query.get(active_sid) is not None


# ---------------------------------------------------------------------------
# Session cookie security flags (finding 3.4)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registration rate limiting (finding 3.9)
# ---------------------------------------------------------------------------

class TestRegistrationRateLimit:

    def test_api_register_blocked_after_10_requests(self, enc_client):
        """11th POST to /api/auth/register from the same IP must return 429."""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        import base64

        def _register():
            key = ec.generate_private_key(ec.SECP256R1())
            pub_b64 = base64.b64encode(
                key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            ).decode()
            return enc_client.post("/api/auth/register",
                                   json={"therapy_mode": "solo", "public_key": pub_b64})

        for _ in range(10):
            resp = _register()
            assert resp.status_code == 201

        resp = _register()
        assert resp.status_code == 429

    def test_legacy_register_blocked_after_10_requests(self, enc_client):
        """11th POST to /auth/solo from the same IP must return 429."""
        for _ in range(10):
            resp = enc_client.post("/auth/solo")
            assert resp.status_code in (200, 302)   # redirect on success

        resp = enc_client.post("/auth/solo")
        assert resp.status_code == 429


class TestSessionCookieConfig:
    def test_httponly_flag_is_enabled(self):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True

    def test_samesite_is_lax(self):
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_secure_flag_off_in_test_mode(self):
        # IS_PRODUCTION is False in tests (SQLite + TESTING=1) so the test
        # client (plain HTTP) can still set and read cookies.
        assert app.config["SESSION_COOKIE_SECURE"] is False

    def test_permanent_session_lifetime_is_30_days(self):
        assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(days=30)

    def test_response_cookie_has_httponly_and_samesite(self, enc_client):
        """Actual Set-Cookie header must carry HttpOnly and SameSite=Lax."""
        # /auth/solo POST sets session["user_id"] with no required fields
        resp = enc_client.post("/auth/solo")
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie


class TestValidateConfigEncryptionKey:
    def test_raises_when_field_encryption_key_missing(self):
        base_env = {
            "TESTING": "0",
            "SECRET_KEY": "abc",
            "ANTHROPIC_API_KEY": "xyz",
            "FIELD_ENCRYPTION_KEY": "",
            "DATABASE_URL": "sqlite:///test.db",
        }
        with patch.dict(os.environ, base_env, clear=True):
            import config as cfg
            importlib.reload(cfg)
            with pytest.raises(RuntimeError, match="FIELD_ENCRYPTION_KEY"):
                cfg.validate_config()

    def test_no_error_when_field_encryption_key_present(self):
        base_env = {
            "TESTING": "0",
            "SECRET_KEY": "abc",
            "ANTHROPIC_API_KEY": "xyz",
            "FIELD_ENCRYPTION_KEY": TEST_KEY,
            "DATABASE_URL": "sqlite:///test.db",
        }
        with patch.dict(os.environ, base_env, clear=True):
            import config as cfg
            importlib.reload(cfg)
            cfg.validate_config()  # must not raise
