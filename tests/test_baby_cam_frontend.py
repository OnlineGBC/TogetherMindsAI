"""
tests/test_baby_cam_frontend.py
-------------------------------
The baby cam is based on the Group session. These tests cover the pieces that are
specific to that: the single "Start watching" button starts a Group session, and
the room for a caregiver's Group session is headed "Baby Cam" (not "Group Circle")
and is still a watch-only monitor. Plus the small wording removals that went with
it: billing feature lists, the "unable to join" page, the session summary page.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-babycam")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession

init_encryption(TEST_KEY)

ROOT = os.path.dirname(os.path.dirname(__file__))


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


def _shows(body, token):
    """True if `token` is on the page. A bare substring test is flaky for short
    numbers like 988: every page carries random tokens (the CSP nonce, the CSRF
    token) that contain '988' by chance now and then. A number only counts when it
    stands on its own."""
    import re
    if token.isdigit():
        return re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % token, body) is not None
    return token in body


def _login(client, role=roles.CAREGIVER, uid="parent"):
    with app.app_context():
        db.session.add(Clinician(
            id=uid, provider="google", provider_subject=uid, role=role,
            email=f"{uid}@example.com", created_at=datetime.now(timezone.utc)))
        db.session.commit()
    with client.session_transaction() as s:
        s["clinician_id"] = uid
        s["user_id"] = uid


def _group_room(client, role):
    """Render a Group session room for an owner with this role."""
    _login(client, role=role)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(TherapySession(
            id="g1", mode="group", created_by="parent", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="parent"))
        db.session.commit()
    with patch.object(config, "RTC_ENABLED", True), \
         patch.object(config, "RECORDING_ENABLED", True):
        return client.get("/therapy/group/g1").get_data(as_text=True)


# ---------------------------------------------------------------------------
# The start page: one button, and it starts a Group session
# ---------------------------------------------------------------------------

def test_start_watching_starts_a_group_session(client):
    _login(client)
    body = client.get("/therapist").get_data(as_text=True)
    assert 'action="/therapist/start/group"' in body
    assert "Start watching" in body
    assert "/therapist/start/solo" not in body
    assert "/therapist/start/couple" not in body


def test_start_watching_creates_a_group_session_owned_by_the_caller(client):
    _login(client)
    with client.session_transaction() as s:
        s["csrf_token"] = "t"
    rv = client.post("/therapist/start/group", data={"csrf_token": "t"})
    assert rv.status_code == 302
    assert "/therapy/group/" in rv.headers["Location"]
    with app.app_context():
        ts = TherapySession.query.one()
        assert ts.mode == "group" and ts.therapist_id == "parent"


def test_start_page_drops_the_therapy_items(client):
    _login(client)
    body = client.get("/therapist").get_data(as_text=True).split("<main", 1)[-1]
    for gone in ("Live risk alerts", "Download transcript", "Lead the session",
                 "Couple Session", "Group Session", "1:1 Session"):
        assert gone not in body, gone


def test_session_list_has_no_transcript_download(client):
    _login(client)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(TherapySession(
            id="g1", mode="group", created_by="parent", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="parent"))
        db.session.commit()
    body = client.get("/therapist").get_data(as_text=True)
    assert "g1" in body                        # the session is listed
    assert "/transcript/g1/pdf" not in body    # with no transcript download


# ---------------------------------------------------------------------------
# The Group room for a caregiver
# ---------------------------------------------------------------------------

def test_caregiver_group_room_is_headed_baby_cam(client):
    html = _group_room(client, roles.CAREGIVER)
    assert ">Baby Cam</h4>" in html
    assert "Group Circle" not in html
    assert "<title>Baby Cam" in html


def test_caregiver_group_room_is_still_a_watch_only_monitor(client):
    html = _group_room(client, roles.CAREGIVER)
    assert 'id="rtcVideoGrid"' in html          # the video
    assert 'id="chatBox"' not in html           # no chat
    assert 'id="rtcMuteBtn"' not in html        # nothing of the watcher's own
    assert 'id="rtcCamBtn"' not in html


def test_other_roles_keep_their_own_group_header(client):
    html = _group_room(client, roles.PSYCHOTHERAPIST)
    assert "Group Circle" in html
    assert ">Baby Cam</h4>" not in html


def test_a_group_session_has_no_camera_limit():
    """The reason to base the baby cam on Group: any number of cameras."""
    assert tm._MODE_CLIENT_CAP.get("group") is None
    assert not tm._session_is_full("g1", "group", "cam-9", "parent")


def test_only_the_owner_can_watch_in_a_group_session(client):
    """Group allows many cameras but one watching login: anyone else publishes
    their own picture and receives nobody's."""
    _login(client)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(TherapySession(
            id="g1", mode="group", created_by="parent", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="parent"))
        db.session.commit()
        assert tm._livekit_media_rights("g1", "parent") == (False, True)
        assert tm._livekit_media_rights("g1", "someone-else") == (True, False)


# ---------------------------------------------------------------------------
# The small wording removals
# ---------------------------------------------------------------------------

def test_billing_lists_show_video_only_for_every_role(client):
    for role in roles.ROLES:
        with app.app_context():
            db.session.query(Clinician).delete()
            db.session.commit()
        _login(client, role=role)
        body = client.get("/pricing").get_data(as_text=True)
        for gone in ("ICD and billing codes", "AI co-pilot suggestions",
                     "AI session recap", "Full session transcript",
                     "Guided reflections chat", "Safety and risk alerts"):
            assert gone not in body, (role, gone)
        assert "Live audio and video" in body, role


def test_the_unable_to_join_page_has_no_crisis_text(client):
    with open(os.path.join(ROOT, "templates", "state_blocked.html"),
              encoding="utf-8") as f:
        src = f.read()
    for gone in ("988", "741741", "in crisis", "findahelpline", "befrienders"):
        assert gone not in src, gone


def test_the_welcome_page_no_longer_points_to_a_crisis_bar(client):
    body = client.get("/welcome").get_data(as_text=True)
    assert "Crisis resources are" not in body
    assert "disclaimer-bar" not in body


def test_the_summary_page_has_no_clinician_disclaimer():
    with open(os.path.join(ROOT, "templates", "session_summary_page.html"),
              encoding="utf-8") as f:
        assert "payload.disclaimer" not in f.read()


def test_no_page_shows_the_crisis_bar_or_the_wellness_popup(client):
    for path in ("/welcome", "/privacy", "/tos", "/session/join", "/pricing"):
        body = client.get(path).get_data(as_text=True)
        for gone in ("disclaimer-bar", "wellnessModal", "Checking in with you",
                     "988", "741741", "findahelpline.com"):
            assert not _shows(body, gone), (path, gone)


# ---------------------------------------------------------------------------
# Sign-in, consent, footer, welcome, start page, room icon
# ---------------------------------------------------------------------------

def _main(html):
    return html.split("<main", 1)[-1].split("</main>", 1)[0]


def _text(html):
    import re
    return re.sub(r"<style.*?</style>|<[^>]+>", " ", html, flags=re.S).lower()


def test_sign_in_screen_is_baby_cam_wording(client):
    with client.session_transaction() as s:
        s["pending_group_session"] = "g1"
    main = _main(client.get("/auth/group").get_data(as_text=True))
    assert "Getting Started</h3>" in main
    assert "Baby cam session" in main
    assert "Private" in main
    for gone in ("Group Circle", "1:1 Session", "Couple Check-in", "licensed professional",
                 "clinical record", "co-pilot", "led by", "used to train AI",
                 "Share the generated Session ID with your group"):
        assert gone not in main, gone


def test_consent_screen_has_no_ai_or_clinician_claims(client):
    _login(client)
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(TherapySession(
            id="g1", mode="group", created_by="parent", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="parent"))
        db.session.commit()
    with client.session_transaction() as s:
        s["user_id"] = "camera-1"
        s.pop("clinician_id", None)
    body = _main(client.get("/session/g1/consent").get_data(as_text=True))
    text = _text(body)
    assert "sending live video and sound" in text
    for gone in ("transcri", "co-pilot", "hipaa", "clinician", "ai transcription"):
        assert gone not in text, gone
    # a caregiver session never asks for a location
    assert "stateSelect" not in body and "locationAttest" not in body


def test_footer_is_just_the_name_and_the_legal_links(client):
    body = client.get("/welcome").get_data(as_text=True)
    footer = body.split("<footer", 1)[-1].split("</footer>", 1)[0]
    assert "TogetherMindsAI" in footer
    assert 'href="/privacy"' in footer and 'href="/tos"' in footer
    for gone in ("AI co-pilot", "clinician-led", "About the AI", "about-the-ai"):
        assert gone not in footer, gone


def test_welcome_page_is_baby_cam_wording(client):
    text = _text(_main(client.get("/welcome").get_data(as_text=True)))
    assert "baby cam" in text
    assert "start watching" in text
    assert "setting up the baby" in text
    for gone in ("clinician", "co-pilot", "assistive aid", "sentiment", "ai-assisted",
                 "anthropic", "claude", "therapy"):
        assert gone not in text, gone


def test_start_page_has_no_checkbox_and_the_button_is_ready(client):
    """Option C: no confirmation box and no locked button on the start page. The
    saved confirmation is the one asked for just before recording."""
    import re
    _login(client)
    main = _main(client.get("/therapist").get_data(as_text=True))
    assert 'type="checkbox"' not in main
    assert "enable the options below" not in main
    assert "Confirm the statement above" not in main
    btn = re.search(r'<button[^>]*therapist-start-btn[^>]*>', main).group(0)
    assert "disabled" not in btn


def test_start_watching_needs_no_confirmation_field(client):
    _login(client)
    with client.session_transaction() as s:
        s["csrf_token"] = "t"
    rv = client.post("/therapist/start/group", data={"csrf_token": "t"})
    assert rv.status_code == 302 and "/therapy/group/" in rv.headers["Location"]


def test_caregiver_room_header_uses_a_camera_icon(client):
    html = _group_room(client, roles.CAREGIVER)
    assert "bi-camera-video-fill text-primary-green fs-3" in html
    assert "bi-person-lines-fill text-primary-green fs-3" not in html


def test_other_roles_keep_the_group_icon_in_the_room_header(client):
    html = _group_room(client, roles.PSYCHOTHERAPIST)
    assert "bi-person-lines-fill text-primary-green fs-3" in html
