"""
tests/test_baby_cam_frontend.py
-------------------------------
The app is a baby cam. Its pages must carry no therapy, clinician, crisis-line or
911/988 wording, and the start page must offer one thing: start watching.

Front-end (template) behaviour only. The video session, pairing and sign-in are
covered by test_caregiver_room.py and the auth tests.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-babycam")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician

init_encryption(TEST_KEY)

ROOT = os.path.dirname(os.path.dirname(__file__))

CRISIS = ("988", "911", "741741", "findahelpline.com", "befrienders.org",
          "disclaimer-bar", "wellnessModal", "Checking in with you")


@pytest.fixture
def client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _login(client, role=roles.CAREGIVER, uid="parent"):
    with app.app_context():
        db.session.add(Clinician(
            id=uid, provider="google", provider_subject=uid, role=role,
            email=f"{uid}@example.com", created_at=datetime.now(timezone.utc)))
        db.session.commit()
    with client.session_transaction() as s:
        s["clinician_id"] = uid
        s["user_id"] = uid


def _main(html):
    return html.split("<main", 1)[-1].split("</main>", 1)[0]


# ---------------------------------------------------------------------------
# The start page offers one thing
# ---------------------------------------------------------------------------

def test_start_page_offers_only_start_watching(client):
    _login(client)
    body = client.get("/therapist").get_data(as_text=True)
    assert 'action="/therapist/start/solo"' in body
    assert "Start watching" in body
    # No couple / group formats and no other session types.
    assert "/therapist/start/couple" not in body
    assert "/therapist/start/group" not in body
    assert "Couple Session" not in body
    assert "Group Session" not in body


def test_start_page_has_no_therapy_wording(client):
    _login(client)
    body = _main(client.get("/therapist").get_data(as_text=True))
    for gone in ("Live risk alerts", "Lead the session", "co-pilot", "Co-Pilot",
                 "your client", "transcript", "AI assists"):
        assert gone not in body, gone


def test_start_page_has_no_crisis_wording_for_any_role(client):
    for role in roles.ROLES:
        with app.app_context():
            db.session.query(Clinician).delete()
            db.session.commit()
        _login(client, role=role)
        body = client.get("/therapist").get_data(as_text=True)
        for gone in CRISIS:
            assert gone not in body, (role, gone)


# ---------------------------------------------------------------------------
# The role picker only offers the baby cam
# ---------------------------------------------------------------------------

def test_role_picker_sends_only_the_caregiver_role(client):
    with app.app_context():
        db.session.add(Clinician(
            id="new", provider="google", provider_subject="new", role=None,
            email="new@example.com", created_at=datetime.now(timezone.utc)))
        db.session.commit()
    with client.session_transaction() as s:
        s["clinician_id"] = "new"
        s["user_id"] = "new"
    body = client.get("/choose-role").get_data(as_text=True)
    assert 'name="role" value="caregiver"' in body
    assert 'type="radio"' not in body
    assert "Psychotherapist" not in body and "Hypnotherapist" not in body


def test_role_picker_submits_and_sets_the_caregiver_role(client):
    with app.app_context():
        db.session.add(Clinician(
            id="new", provider="google", provider_subject="new", role=None,
            email="new@example.com", created_at=datetime.now(timezone.utc)))
        db.session.commit()
    with client.session_transaction() as s:
        s["clinician_id"] = "new"
        s["user_id"] = "new"
        s["csrf_token"] = "t"
    rv = client.post("/choose-role", data={"role": "caregiver", "next": "",
                                           "csrf_token": "t"})
    assert rv.status_code == 302
    with app.app_context():
        assert db.session.get(Clinician, "new").role == roles.CAREGIVER


# ---------------------------------------------------------------------------
# The landing page and the footer
# ---------------------------------------------------------------------------

def test_landing_page_is_a_baby_cam_with_no_crisis_wording(client):
    import re
    body = client.get("/welcome").get_data(as_text=True)
    assert "baby cam" in body.lower()
    assert 'href="/session/join"' in body
    # Words on the page, not CSS class names.
    text = re.sub(r"<style.*?</style>|<[^>]+>", " ", _main(body), flags=re.S).lower()
    for gone in ("988", "911", "741741", "crisis", "clinician", "co-pilot",
                 "sentiment", "therapy"):
        assert gone not in text, gone
    for gone in CRISIS:
        assert gone not in body, gone


def test_footer_no_longer_mentions_the_ai_copilot(client):
    body = client.get("/welcome").get_data(as_text=True)
    assert "AI co-pilot for clinician-led sessions" not in body
    assert "About the AI" not in body


# ---------------------------------------------------------------------------
# The manifest and the summary page
# ---------------------------------------------------------------------------

def test_manifest_describes_a_baby_cam():
    with open(os.path.join(ROOT, "static", "manifest.json"), encoding="utf-8") as f:
        desc = json.load(f)["description"].lower()
    assert "baby" in desc
    assert "therapy" not in desc


def test_summary_page_template_has_no_clinician_disclaimer():
    with open(os.path.join(ROOT, "templates", "session_summary_page.html"),
              encoding="utf-8") as f:
        assert "payload.disclaimer" not in f.read()


# ---------------------------------------------------------------------------
# The sign-in, consent and legal pages
# ---------------------------------------------------------------------------

def test_the_pages_a_watched_device_sees_carry_no_crisis_lines(client):
    with client.session_transaction() as s:
        s["pending_solo_session"] = "sess-1"
    for path in ("/auth/solo", "/tos", "/privacy", "/session/join"):
        body = client.get(path).get_data(as_text=True)
        for gone in CRISIS:
            assert gone not in body, (path, gone)
