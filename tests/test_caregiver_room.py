"""
tests/test_caregiver_room.py
----------------------------
Step 5: the caregiver session room is a monitor, not a conversation.

Live video and a record button; no chat, no transcript, no captions, no
speech-to-text. The room is driven by the ROLE's capability list, not by a role
name, so it follows the same table as every other gate.

The subtle risk here is not the HTML — it is the JavaScript. The page attaches
listeners to chat elements, and speech-to-text starts automatically on join. If
those run when the elements are missing, the script throws and takes the video
setup down with it. Several tests below exist for that reason alone.
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
os.environ.setdefault("SECRET_KEY", "test-secret-caregiver")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession

init_encryption(TEST_KEY)


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


def _room(client, role, rtc=True, recording=True):
    """Render the session room for a practitioner with this role.

    RTC and recording are forced on: without LiveKit credentials the test config
    turns them off, and the video pane this step is about would never render.
    """
    with app.app_context():
        db.session.query(Clinician).delete()
        db.session.query(TherapySession).delete()
        now = datetime.now(timezone.utc)
        db.session.add(Clinician(id="doc", provider="google", provider_subject="doc",
                                 email="doc@example.com", role=role, created_at=now))
        db.session.add(TherapySession(
            id="s1", mode="solo", created_by="doc", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="doc"))
        db.session.commit()
    with client.session_transaction() as s:
        s["user_id"] = "doc"
        s["clinician_id"] = "doc"
    with patch.object(config, "RTC_ENABLED", rtc), \
         patch.object(config, "RECORDING_ENABLED", recording):
        return client.get("/therapy/solo/s1").get_data(as_text=True)


# ---------------------------------------------------------------------------
# What a caregiver's room does and does not contain
# ---------------------------------------------------------------------------

def test_caregiver_room_has_no_chat(client):
    html = _room(client, roles.CAREGIVER)
    assert 'id="chatBox"' not in html
    assert 'id="messageInput"' not in html
    assert 'id="sendBtn"' not in html


def test_caregiver_room_has_no_transcription_controls(client):
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcSttBtn"' not in html
    assert 'id="rtcCaptions"' not in html


def test_caregiver_room_still_has_video(client):
    """Watching live IS the caregiver product. It must survive the stripping."""
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcVideoGrid"' in html
    assert 'id="rtcMuteBtn"' in html
    assert 'id="rtcCamBtn"' in html


def test_caregiver_room_still_has_recording(client):
    """Recording is the thing they pay for."""
    html = _room(client, roles.CAREGIVER)
    assert 'id="recRequestBtn"' in html


def test_a_clinical_room_is_unchanged(client):
    """The stripping must not leak into the roles that do have these features."""
    html = _room(client, roles.PSYCHOTHERAPIST)
    for element in ('id="chatBox"', 'id="messageInput"', 'id="sendBtn"',
                    'id="rtcSttBtn"', 'id="rtcCaptions"', 'id="rtcVideoGrid"'):
        assert element in html, element


def test_a_coachs_room_keeps_chat_and_transcript(client):
    html = _room(client, roles.HYPNOTHERAPIST)
    assert 'id="chatBox"' in html
    assert 'id="rtcSttBtn"' in html


# ---------------------------------------------------------------------------
# The JavaScript must not blow up on the missing elements
# ---------------------------------------------------------------------------

def test_the_page_tells_the_script_what_it_has(client):
    html = _room(client, roles.CAREGIVER)
    assert "var HAS_CHAT       = false" in html
    assert "var HAS_TRANSCRIPT = false" in html


def test_speech_to_text_does_not_auto_start_without_a_transcript(client):
    """It would bill for speech-to-text nobody receives, and write into caption
    elements this page never renders."""
    html = _room(client, roles.CAREGIVER)
    assert "if (HAS_TRANSCRIPT) { startSTT(); }" in html
    assert "sttEnabled = HAS_TRANSCRIPT;" in html


def test_chat_listeners_are_guarded(client):
    """An unguarded addEventListener on a missing element throws, and everything
    after it in that script block never runs — including the video setup."""
    html = _room(client, roles.CAREGIVER)
    assert "if (_sendBtn && _msgInput) {" in html


def test_caption_writes_are_guarded():
    """Reached only through the speech socket, but a role change mid-session
    could still get there with no captions element on the page."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "templates", "session_live.html"),
               encoding="utf-8").read()
    # Every write to the captions element sits behind a null check on the same
    # line — either "if (captionsEl)" or "if (captionsEl && ...)".
    for line in src.splitlines():
        if "captionsEl.textContent" in line:
            assert "if (captionsEl" in line, line.strip()


# ---------------------------------------------------------------------------
# The session-ID strip: labels, not tooltips
# ---------------------------------------------------------------------------

def test_the_icon_buttons_carry_their_words(client):
    """These three were icon-only, labelled by a `title` tooltip alone. A tooltip
    needs a mouse to hover, so on a phone or tablet the label never appeared at
    all and they were three unexplained icons."""
    html = _room(client, roles.PSYCHOTHERAPIST)
    for word in ("Copy", "Name", "Rename"):
        assert f'<span class="ms-1">{word}</span>' in html, word


def test_the_icon_buttons_are_labelled_for_screen_readers(client):
    """`title` alone is weak for assistive tech — an explicit aria-label is not."""
    html = _room(client, roles.PSYCHOTHERAPIST)
    for label in ("Copy Session ID", "Name this session", "Change display name"):
        assert f'aria-label="{label}"' in html, label


# ---------------------------------------------------------------------------
# Brighten: a viewing aid for the role that watches a dim room
# ---------------------------------------------------------------------------

def test_the_caregiver_room_offers_brighten(client):
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcBrightenSelect"' in html
    for level in ('value="off"', 'value="low"', 'value="high"'):
        assert level in html


def test_brighten_says_plainly_that_it_is_not_night_vision(client):
    """Without this it gets reported as "night vision is broken". It lifts a dim
    picture; it cannot show what the camera never captured."""
    html = _room(client, roles.CAREGIVER)
    assert "cannot see in" in html and "infrared" in html


def test_other_roles_do_not_get_brighten(client):
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        html = _room(client, role)
        assert 'id="rtcBrightenSelect"' not in html, role


def test_brighten_never_touches_the_self_view():
    """You must always see your own camera as it really is — and the filter must
    stay off the sender, where it would clash with the background processor."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    for level in ("brighten-low", "brighten-high"):
        line = [l for l in css.splitlines() if level in l and "filter" in l]
        assert line, level
        assert ':not([data-tile="local"])' in line[0], level
