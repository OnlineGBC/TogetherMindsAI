"""
tests/test_carecam_rename.py
----------------------------
The product is CareCam Secure: one name for watching over a baby, an elder or anyone
else. These tests cover the rename on the pages and in the emails, the "person you
are watching over" wording in place of "your baby", and the caregiver-only email text.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import re
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-carecam")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())

import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app

ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _text(html):
    return re.sub(r"<style.*?</style>|<script.*?</script>|<[^>]+>", " ", html,
                  flags=re.S)


def _template(name):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/welcome", "/tos", "/privacy"])
def test_public_pages_use_the_new_name(client, path):
    body = client.get(path).get_data(as_text=True)
    text = _text(body)
    assert "CareCam Secure" in text
    assert "TogetherMindsAI" not in text
    assert "BabyCamSecure" not in text


@pytest.mark.parametrize("path", ["/welcome", "/tos", "/privacy"])
def test_public_pages_do_not_say_baby(client, path):
    text = _text(client.get(path).get_data(as_text=True)).lower()
    assert "baby" not in text


def test_welcome_page_speaks_of_the_person_being_watched(client):
    text = _text(client.get("/welcome").get_data(as_text=True))
    assert "Watch over someone you care for" in text
    assert "Setting up the camera?" in text


def test_terms_describe_a_care_camera(client):
    text = _text(client.get("/tos").get_data(as_text=True))
    assert "CareCam Secure is a care camera" in text
    assert "the room of the person you are watching over" in text


@pytest.mark.parametrize("name", ["auth.html", "therapist.html", "base.html",
                                  "session_live.html", "consent_gate.html",
                                  "my_sessions.html", "session_ended.html",
                                  "billing.html", "404.html", "login.html",
                                  "client_login.html"])
def test_templates_carry_no_old_product_name(name):
    src = _template(name)
    assert "TogetherMindsAI" not in src.replace("togethermindsai@", "")
    assert "BabyCamSecure" not in src


def test_start_page_and_room_header_use_carecam():
    start = _template("therapist.html")
    assert "Start watching — CareCam Secure" in start
    assert "Baby Cam" not in start and "baby" not in start.lower()
    assert "Baby Cam" not in _template("session_live.html")


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def _recording_row():
    return SimpleNamespace(
        session_id="s-1", download_token="tokC",
        retention_expires_at=datetime.now(timezone.utc) + timedelta(days=30))


def _recording_email(role, kind="ready"):
    with app.app_context(), \
         patch.object(tm, "_session_clinician", return_value=None), \
         patch.object(roles, "role_of", return_value=role):
        return tm._recording_email_content(_recording_row(), kind)


@pytest.mark.parametrize("kind", ["ready", "early", "reminder"])
def test_recording_email_subject_uses_the_new_name(kind):
    subject, _, _ = _recording_email(roles.CAREGIVER, kind)
    assert subject.startswith("CareCam Secure — ")


@pytest.mark.parametrize("kind", ["ready", "reminder"])
def test_caregiver_recording_email_has_no_practice_or_clinical_text(kind):
    _, plain, html = _recording_email(roles.CAREGIVER, kind)
    for body in (plain, html):
        assert "practice" not in body
        assert "clinical" not in body.lower()
        assert "six years" not in body
        assert "clinician" not in body.lower()
        assert "the account that started this session" in body
        assert "cannot be recovered" in body
        assert "TogetherMindsAI" not in body


def test_other_roles_keep_the_retention_bullets():
    _, plain, html = _recording_email(roles.PSYCHOTHERAPIST)
    for body in (plain, html):
        assert "retained for six years" in body
        assert "practice's requirements" in body or "practice&#x27;s requirements" in body


def test_email_footer_and_from_name_use_the_new_name():
    assert tm._sent_at_line().startswith("Sent automatically by CareCam Secure at ")
    with patch("TogetherMindsAI.smtplib") as mock_smtplib, \
         patch.object(tm.config, "FEEDBACK_FROM_EMAIL", "noreply@example.com"), \
         patch.object(tm.config, "FEEDBACK_SMTP_HOST", "smtp.example.com"), \
         patch.object(tm.config, "FEEDBACK_SMTP_PORT", 587), \
         patch.object(tm.config, "FEEDBACK_SMTP_USER", "u"), \
         patch.object(tm.config, "FEEDBACK_SMTP_PASSWORD", "p"):
        smtp = mock_smtplib.SMTP.return_value.__enter__.return_value
        tm._send_email(["a@example.com"], "S", "plain", "<p>html</p>")
    assert smtp.send_message.call_args[0][0]["From"] == "CareCam Secure <noreply@example.com>"


def _transcript_email(role):
    ts = SimpleNamespace(id="s-1", download_token="tokT")
    with app.app_context(), \
         patch.object(tm, "_session_clinician", return_value=None), \
         patch.object(roles, "role_of", return_value=role):
        return tm._session_email_content(ts)


def test_caregiver_transcript_email_has_no_analysis_or_icd_or_clinician():
    subject, plain, html = _transcript_email(roles.CAREGIVER)
    assert subject.startswith("CareCam Secure — ")
    for body in (plain, html):
        assert "ICD" not in body
        assert "AI analysis" not in body
        assert "clinician" not in body.lower()
        assert "the account that started this session" in body
        assert "/session/transcript/tokT/pdf" in body
        assert "/session/transcript/tokT/docx" in body


def test_other_roles_keep_analysis_and_icd_in_the_transcript_email():
    _, plain, html = _transcript_email(roles.PSYCHOTHERAPIST)
    assert "ICD codes" in plain
    assert "ICD codes" in html


def test_caregiver_disclaimer_uses_the_new_name():
    assert roles.words(roles.CAREGIVER)["disclaimer_lead"].startswith("CareCam Secure")


def test_watch_from_another_room_or_across_the_world(client):
    welcome = _text(client.get("/welcome").get_data(as_text=True))
    assert "from another room or across the world" in welcome
    assert "from another room or across the world." in _template("therapist.html")
