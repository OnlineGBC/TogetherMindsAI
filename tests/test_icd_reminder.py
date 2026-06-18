"""
tests/test_icd_reminder.py
--------------------------
The annual ICD code-refresh reminder email (sent by the app every March 1):

  - the runbook body carries the key steps (API creds in .env + Google Secret
    Manager, the harvest script, verify, commit);
  - it sends via the shared SMTP path when creds are configured;
  - it no-ops gracefully (no send, no raise) when SMTP creds are absent.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-icd-reminder")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

import config as _config
import TogetherMindsAI as tm


def test_reminder_content_has_runbook_markers():
    subject, plain, html_body = tm._icd_refresh_reminder_content()
    assert "ICD" in subject
    for marker in ("scripts/harvest_icd11_entities.py", "--write", ".env",
                   "Google Secret Manager", "WHO_ICD_ClientId", "WHO_ICD_ClientSecret"):
        assert marker in plain, f"plain body missing: {marker}"
    # ICD-10 is explicitly called out as no-refresh-needed.
    assert "search-by-code" in plain
    # The HTML alternative is populated too.
    assert "Annual ICD code refresh" in html_body


def test_reminder_sends_when_creds_present():
    with patch.object(_config, "FEEDBACK_SMTP_USER", "sender@example.com"), \
         patch.object(_config, "FEEDBACK_SMTP_PASSWORD", "app-password"), \
         patch.object(tm, "_send_feedback_email") as send:
        tm._send_icd_refresh_reminder()
    send.assert_called_once()
    subject, plain, html_body = send.call_args[0]
    assert "harvest_icd11_entities" in plain


def test_reminder_noops_when_creds_absent():
    with patch.object(_config, "FEEDBACK_SMTP_USER", ""), \
         patch.object(_config, "FEEDBACK_SMTP_PASSWORD", ""), \
         patch.object(tm, "_send_feedback_email") as send:
        tm._send_icd_refresh_reminder()          # must not raise
    send.assert_not_called()
