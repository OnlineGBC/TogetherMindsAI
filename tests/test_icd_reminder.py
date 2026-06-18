"""
tests/test_icd_reminder.py
--------------------------
The annual ICD code-refresh reminder the app emails itself:

  - the runbook body carries the key steps (API creds in .env + Google Secret
    Manager, the harvest script, verify, commit);
  - delivery is exactly-once per year via the NotificationLog ledger (claim then
    send), and a send failure releases the claim so a later run retries;
  - it only starts in 2027, no-ops without SMTP creds;
  - the startup catch-up fires on/after March 1 and not before.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-icd-reminder")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import config as _config
import TogetherMindsAI as tm
from models import db, init_encryption, NotificationLog

init_encryption(TEST_KEY)


@pytest.fixture
def app_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[tm.app] = {None: engine}
    tm.app.config["TESTING"] = True
    with tm.app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def _creds_on():
    return [
        patch.object(_config, "FEEDBACK_SMTP_USER", "sender@example.com"),
        patch.object(_config, "FEEDBACK_SMTP_PASSWORD", "app-password"),
    ]


def _rows():
    return NotificationLog.query.filter_by(key=tm._ICD_REMINDER_KEY).count()


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def test_reminder_content_has_runbook_markers():
    subject, plain, html_body = tm._icd_refresh_reminder_content()
    assert "ICD" in subject
    for marker in ("scripts/harvest_icd11_entities.py", "--write", ".env",
                   "Google Secret Manager", "WHO_ICD_ClientId", "WHO_ICD_ClientSecret"):
        assert marker in plain, f"plain body missing: {marker}"
    assert "search-by-code" in plain
    assert "Annual ICD code refresh" in html_body


# ---------------------------------------------------------------------------
# Exactly-once delivery
# ---------------------------------------------------------------------------

def test_deliver_sends_once_and_records_claim(app_db):
    with _creds_on()[0], _creds_on()[1], patch.object(tm, "_send_feedback_email") as send:
        tm._deliver_icd_reminder(2027)
        tm._deliver_icd_reminder(2027)          # second call must be a no-op
    send.assert_called_once()
    assert _rows() == 1


def test_deliver_skips_before_start_year(app_db):
    with _creds_on()[0], _creds_on()[1], patch.object(tm, "_send_feedback_email") as send:
        tm._deliver_icd_reminder(2026)
    send.assert_not_called()
    assert _rows() == 0


def test_deliver_noops_without_creds(app_db):
    with patch.object(_config, "FEEDBACK_SMTP_USER", ""), \
         patch.object(_config, "FEEDBACK_SMTP_PASSWORD", ""), \
         patch.object(tm, "_send_feedback_email") as send:
        tm._deliver_icd_reminder(2027)
    send.assert_not_called()
    assert _rows() == 0                          # no claim made when it can't send


def test_deliver_releases_claim_on_send_failure(app_db):
    with _creds_on()[0], _creds_on()[1], \
         patch.object(tm, "_send_feedback_email", side_effect=RuntimeError("smtp down")):
        tm._deliver_icd_reminder(2027)           # must not raise
    assert _rows() == 0                          # claim released so a later run retries

    # A subsequent healthy run then succeeds and claims.
    with _creds_on()[0], _creds_on()[1], patch.object(tm, "_send_feedback_email") as send:
        tm._deliver_icd_reminder(2027)
    send.assert_called_once()
    assert _rows() == 1


# ---------------------------------------------------------------------------
# Startup catch-up date gate
# ---------------------------------------------------------------------------

def _fixed_now(dt):
    m = MagicMock(wraps=datetime)
    m.now.return_value = dt
    return m


def test_catchup_sends_on_or_after_march1():
    with patch.object(tm, "_deliver_icd_reminder") as deliver, \
         patch.object(tm, "datetime", _fixed_now(datetime(2027, 3, 15, tzinfo=timezone.utc))):
        tm._icd_reminder_catchup()
    deliver.assert_called_once_with(2027)


def test_catchup_skips_before_march1():
    with patch.object(tm, "_deliver_icd_reminder") as deliver, \
         patch.object(tm, "datetime", _fixed_now(datetime(2027, 2, 15, tzinfo=timezone.utc))):
        tm._icd_reminder_catchup()
    deliver.assert_not_called()
