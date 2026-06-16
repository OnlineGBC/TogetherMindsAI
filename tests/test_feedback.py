"""
tests/test_feedback.py
----------------------
Coverage for the email-only feedback form.

Privacy contract (FEEDBACK_FORM_PLAN.md): no IP, no user_id, no session content
captured anywhere on this code path. The privacy contract test below is the
specific guard required by CLAUDE.md.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
import pytest
from unittest.mock import patch, MagicMock

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-feedback")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

import config as _config
import TogetherMindsAI as tm
from TogetherMindsAI import (
    app,
    _feedback_last_submit,
    _feedback_daily_count,
    _FEEDBACK_TEXT_MAX,
)
from models import db


@pytest.fixture(autouse=True)
def _smtp_creds_present():
    """Most tests assume SMTP creds are configured. Patch them on for every
    test; the missing-creds test overrides this explicitly."""
    with patch.object(_config, "FEEDBACK_SMTP_USER", "test-sender@example.com"), \
         patch.object(_config, "FEEDBACK_SMTP_PASSWORD", "test-app-password"), \
         patch.object(_config, "FEEDBACK_TO_EMAIL", "raja@onlinegbc.com"), \
         patch.object(_config, "FEEDBACK_FROM_EMAIL", "test-sender@example.com"):
        yield


VALID_PAYLOAD = {
    "rating": 4,
    "what_worked": "The reflective prompts felt natural.",
    "what_to_improve": "Faster response times.",
    "desired_features": "Mood timeline.",
    "would_pay": "maybe",
    "other": "Thanks!",
    "platform": "web",
    "mode": "solo",
}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()
    _feedback_last_submit.clear()
    _feedback_daily_count.clear()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def _plain_body(sent_msg):
    return sent_msg.get_body(preferencelist=("plain",)).get_content()


def _html_body(sent_msg):
    part = sent_msg.get_body(preferencelist=("html",))
    return part.get_content() if part else ""


def test_valid_full_submission_sends_email(client):
    with patch("TogetherMindsAI.smtplib") as mock_smtplib:
        mock_smtp = MagicMock()
        mock_smtplib.SMTP.return_value.__enter__.return_value = mock_smtp

        rv = client.post("/api/feedback", json=VALID_PAYLOAD)
        assert rv.status_code == 200, rv.get_json()
        assert rv.get_json() == {"ok": True}

        # send_message called exactly once
        mock_smtp.send_message.assert_called_once()
        sent_msg = mock_smtp.send_message.call_args[0][0]

        # To: tracks the configured recipient list (may be one or several).
        from config import FEEDBACK_TO_EMAILS
        assert sent_msg["To"] == ", ".join(FEEDBACK_TO_EMAILS)
        assert "raja@onlinegbc.com" in sent_msg["To"]
        assert sent_msg["From"] == "test-sender@example.com"
        subject = sent_msg["Subject"]
        assert "Solo" in subject
        assert "4 / 5" in subject

        plain = _plain_body(sent_msg)
        assert "The reflective prompts felt natural." in plain
        assert "Faster response times." in plain
        assert "Mood timeline." in plain
        assert "Thanks!" in plain
        assert "Maybe" in plain

        html = _html_body(sent_msg)
        assert "The reflective prompts felt natural." in html
        assert "Solo Reflection" in html


def test_rating_null_accepted_for_na(client):
    payload = dict(VALID_PAYLOAD, rating=None)
    with patch("TogetherMindsAI.smtplib") as mock_smtplib:
        mock_smtplib.SMTP.return_value.__enter__.return_value = MagicMock()
        rv = client.post("/api/feedback", json=payload)
        assert rv.status_code == 200


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_rating", [0, 6, -1, "abc", 3.5])
def test_invalid_rating_rejected(client, bad_rating):
    payload = dict(VALID_PAYLOAD, rating=bad_rating)
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


@pytest.mark.parametrize("bad_pay", ["YES", "perhaps", "free", 1])
def test_invalid_would_pay_rejected(client, bad_pay):
    payload = dict(VALID_PAYLOAD, would_pay=bad_pay)
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


@pytest.mark.parametrize("bad_platform", ["desktop", "ios", "", None, "browser"])
def test_invalid_platform_rejected(client, bad_platform):
    payload = dict(VALID_PAYLOAD, platform=bad_platform)
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


@pytest.mark.parametrize("bad_mode", ["family", "1on1", 1, ["solo"]])
def test_invalid_mode_rejected(client, bad_mode):
    payload = dict(VALID_PAYLOAD, mode=bad_mode)
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


def test_oversized_text_field_rejected(client):
    payload = dict(VALID_PAYLOAD, what_worked="x" * (_FEEDBACK_TEXT_MAX + 1))
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


def test_all_empty_submission_rejected(client):
    payload = {
        "rating": None,
        "what_worked": "",
        "what_to_improve": "",
        "desired_features": "",
        "would_pay": None,
        "other": "",
        "platform": "web",
        "mode": None,
    }
    rv = client.post("/api/feedback", json=payload)
    assert rv.status_code == 400


# ---------------------------------------------------------------------------
# Rate limiting (cookie-based, no IP)
# ---------------------------------------------------------------------------

def test_cooldown_blocks_rapid_resubmit(client):
    with patch("TogetherMindsAI.smtplib") as mock_smtplib:
        mock_smtplib.SMTP.return_value.__enter__.return_value = MagicMock()

        rv1 = client.post("/api/feedback", json=VALID_PAYLOAD)
        assert rv1.status_code == 200

        rv2 = client.post("/api/feedback", json=VALID_PAYLOAD)
        assert rv2.status_code == 429


# ---------------------------------------------------------------------------
# SMTP failure
# ---------------------------------------------------------------------------

def test_smtp_failure_returns_503(client):
    with patch("TogetherMindsAI.smtplib") as mock_smtplib:
        mock_smtplib.SMTP.side_effect = OSError("connection refused")
        rv = client.post("/api/feedback", json=VALID_PAYLOAD)
        assert rv.status_code == 503

        # Cooldown should NOT advance on failure — user can retry immediately
        with patch("TogetherMindsAI.smtplib") as mock_smtplib2:
            mock_smtplib2.SMTP.return_value.__enter__.return_value = MagicMock()
            rv2 = client.post("/api/feedback", json=VALID_PAYLOAD)
            assert rv2.status_code == 200


# ---------------------------------------------------------------------------
# Privacy contract — the test that protects the no-PII guarantee
# ---------------------------------------------------------------------------

def test_no_pii_leaks_into_email_or_logs(client, caplog):
    """Submit with a fake IP/UA and assert the email body and any logs contain
    none of those identifiers, and no DB row is created in any table."""
    fake_ip = "203.0.113.42"
    fake_ua = "FakeBrowser/1.0 (privacy-test)"

    # Snapshot DB row counts before
    with app.app_context():
        from models import (
            User, ChatMessage, Exercise, AuditLog,
            RateLimitEntry, TherapySession,
        )
        before = {
            "users": User.query.count(),
            "chat_messages": ChatMessage.query.count(),
            "exercises": Exercise.query.count(),
            "audit_logs": AuditLog.query.count(),
            "rate_limit_entries": RateLimitEntry.query.count(),
            "therapy_sessions": TherapySession.query.count(),
        }

    with patch("TogetherMindsAI.smtplib") as mock_smtplib:
        mock_smtp = MagicMock()
        mock_smtplib.SMTP.return_value.__enter__.return_value = mock_smtp

        with caplog.at_level(logging.DEBUG):
            rv = client.post(
                "/api/feedback",
                json=VALID_PAYLOAD,
                environ_overrides={"REMOTE_ADDR": fake_ip},
                headers={"User-Agent": fake_ua},
            )
        assert rv.status_code == 200

        # Email body and headers contain no IP / UA / cookie
        sent_msg = mock_smtp.send_message.call_args[0][0]
        plain = _plain_body(sent_msg)
        html = _html_body(sent_msg)
        assert fake_ip not in plain
        assert fake_ua not in plain
        assert fake_ip not in html
        assert fake_ua not in html
        assert fake_ip not in str(sent_msg)
        assert fake_ua not in str(sent_msg)

        # Logs contain no IP
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert fake_ip not in log_text

    # No DB row created in any table
    with app.app_context():
        after = {
            "users": User.query.count(),
            "chat_messages": ChatMessage.query.count(),
            "exercises": Exercise.query.count(),
            "audit_logs": AuditLog.query.count(),
            "rate_limit_entries": RateLimitEntry.query.count(),
            "therapy_sessions": TherapySession.query.count(),
        }
    assert before == after


def test_source_does_not_reference_remote_addr_in_feedback_handler():
    """Static guard: the feedback endpoint source must not call request.remote_addr.

    This is a belt-and-suspenders check on top of the runtime privacy test
    above — if a future edit accidentally introduces an IP read, this fails.
    """
    import inspect
    src = inspect.getsource(tm.api_feedback)
    assert "remote_addr" not in src
    assert "X-Forwarded-For" not in src
    assert "x-forwarded-for" not in src


# ---------------------------------------------------------------------------
# Standalone page renders
# ---------------------------------------------------------------------------

def test_feedback_page_renders_without_login(client):
    rv = client.get("/feedback")
    assert rv.status_code == 200
    assert b"feedbackForm" in rv.data
    assert b"feedback" in rv.data.lower()


# ---------------------------------------------------------------------------
# Configuration guardrail
# ---------------------------------------------------------------------------

def test_missing_smtp_credentials_returns_503(client):
    with patch.object(_config, "FEEDBACK_SMTP_USER", ""), \
         patch.object(_config, "FEEDBACK_SMTP_PASSWORD", ""):
        rv = client.post("/api/feedback", json=VALID_PAYLOAD)
        assert rv.status_code == 503
