"""
tests/test_consent_wording.py
-----------------------------
What clinicians and clients are told before a session, and the fact that the
session now begins with audio and video already on.

Three gates exist for a client. Two of them matter here:

  /auth/<mode>        "Getting Started" — age, clinician-led, clinical record, ToS
  consent_gate.html   PER SESSION — transcription, recording, retention, HIPAA
                      processors, location. This is the one that is RECORDED:
                      an audit row, a transcript line, and a note to the clinician.

A third block used to sit in the live session page, restating the same
transcription disclosure behind a "Join audio" button. It stored nothing — it only
hid a div — so removing it lost no consent record and removed one click. These
tests hold that line: the recorded gate must keep saying everything it said.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import re
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-consent")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import roles
from TogetherMindsAI import app
from models import db, init_encryption

init_encryption(TEST_KEY)

ROOT = os.path.join(os.path.dirname(__file__), "..")
LIVE = os.path.join(ROOT, "templates", "session_live.html")

AV_CLINICIAN = ("This session will start with audio and video enabled, "
                "although I will be able to turn it off.")


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


def _getting_started(client):
    """The Getting Started page as a client joining a session actually sees it.

    /auth/<mode> exists only to take someone INTO a clinician-led session they are
    joining, so without a pending join it redirects to /session/join and the cards
    never render.
    """
    with client.session_transaction() as s:
        s["pending_solo_session"] = "sess-1"
    return _flat(client.get("/auth/solo").get_data(as_text=True))


def _shows(body, token):
    """True if `token` is on the page. A bare substring test is flaky for short
    numbers like 988: every page carries random tokens (the CSP nonce, the CSRF
    token) that contain '988' by chance now and then. A number only counts when it
    stands on its own."""
    import re
    if token.isdigit():
        return re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % token, body) is not None
    return token in body


def _live_src():
    return open(LIVE, encoding="utf-8").read()


def _flat(html):
    """One line, so wording that wraps in the template still matches."""
    return " ".join(html.split())


# ---------------------------------------------------------------------------
# The client's Getting Started cards
# ---------------------------------------------------------------------------

def test_the_age_card_accepts_a_trusted_adults_permission(client):
    body = _getting_started(client)
    assert "18 years of age or older" in body
    assert "or I have a trusted adult's permission to participate" in body


def test_the_age_card_no_longer_repeats_the_crisis_number(client):
    """Asked for because 988 is already on every page and in the Terms. It must be
    gone from THIS CARD. (The crisis bar and the crisis lines in the Terms have
    since been removed everywhere, so 988 is no longer on the page at all.)"""
    body = _getting_started(client)
    assert "Under 18: please speak with a trusted adult" not in body
    assert not _shows(body, "988")


def test_the_terms_have_no_crisis_numbers(client):
    body = _flat(client.get("/tos").get_data(as_text=True))
    for gone in ("988", "911", "741741", "findahelpline.com", "befrienders.org"):
        assert not _shows(body, gone), gone


def test_the_client_is_told_the_session_starts_with_audio_and_video(client):
    body = _getting_started(client)
    assert "start with <strong>audio and video enabled</strong>" in body
    assert "although I can turn it off" in body


def test_the_third_card_says_nothing_is_saved_unless_recording_is_on(client):
    """The clinical-record card was replaced for the baby cam."""
    body = _getting_started(client)
    assert "Nothing is saved unless the person watching turns" in body
    assert "30 days" in body
    assert "clinical record" not in body


def test_continuing_still_needs_the_box_ticked(client):
    """The wording changed; the gate did not."""
    body = _getting_started(client)
    assert 'id="agreeAll"' in body
    assert "required" in body
    assert 'id="continueBtn"' in body and "disabled" in body


# ---------------------------------------------------------------------------
# The clinician's attestation — every role
# ---------------------------------------------------------------------------

def test_the_clinical_roles_are_told_the_session_starts_with_audio_and_video():
    """Both roles that run a two-way session, so neither is told less than the
    other about how their session begins."""
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        assert AV_CLINICIAN in roles.WORDING[role]["attestation"], role


def test_a_caregiver_is_not_told_their_own_camera_will_start():
    """A monitor room watches SOMEONE ELSE'S camera and publishes none of its own
    (see _wantVideo in session_live.html). The shared sentence would have had a
    caregiver tick a box saying their video starts when it never does."""
    text = roles.WORDING[roles.CAREGIVER]["attestation"]
    assert AV_CLINICIAN not in text
    # Nor their microphone: a caregiver only watches and publishes nothing.
    assert "my microphone enabled" not in text
    assert "my microphone and camera off" in text
    # What actually happens: they watch and listen to the other camera.
    assert "see and hear the camera I am monitoring" in text


def test_no_role_is_left_silent_about_how_the_session_begins():
    """Different wording per role is fine. Saying nothing is not."""
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST, roles.CAREGIVER):
        text = roles.WORDING[role]["attestation"]
        assert "will start with" in text, role


def test_each_role_keeps_what_it_already_attested():
    """The sentence was APPENDED. Losing the original claim would be far worse
    than never adding to it."""
    assert "I am a licensed professional" in roles.WORDING[roles.PSYCHOTHERAPIST]["attestation"]
    assert "responsible for the session" in roles.WORDING[roles.PSYCHOTHERAPIST]["attestation"]
    assert "I am a qualified practitioner" in roles.WORDING[roles.HYPNOTHERAPIST]["attestation"]
    assert "authorised to record this person" in roles.WORDING[roles.CAREGIVER]["attestation"]


def test_the_attestation_is_written_in_the_first_person():
    """It is a statement the clinician makes, not a notice they are given, so
    "I will be able to" — not "you can"."""
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST, roles.CAREGIVER):
        text = roles.WORDING[role]["attestation"]
        assert " I " in " " + text
        assert "you can turn" not in text.lower()


# ---------------------------------------------------------------------------
# The per-session gate — the one that is recorded
# ---------------------------------------------------------------------------

def test_the_gate_says_this_device_sends_video_and_sound():
    src = _flat(open(os.path.join(ROOT, "templates", "consent_gate.html"),
                     encoding="utf-8").read())
    assert "sending live video and sound" in src
    assert "turn the camera or microphone off" in src


def test_the_gate_makes_the_baby_cam_disclosures_and_no_ai_ones():
    """A baby cam has no transcription and no co-pilot, so the gate must not
    claim consent to either."""
    src = _flat(open(os.path.join(ROOT, "templates", "consent_gate.html"),
                     encoding="utf-8").read())
    for claim in ("Nothing is saved unless the person watching turns",
                  "30 days",
                  "locationAttest"):        # the licensed-session block is untouched
        assert claim in src, claim
    for gone in ("transcribed", "AI co-pilot", "HIPAA", "live AI transcription",
                 "up to 6 years"):
        assert gone not in src, gone


def test_the_gate_is_still_required_per_session():
    """Not once per browser. A per-session gate is what makes the recorded consent
    match the session it belongs to."""
    src = open(os.path.join(ROOT, "TogetherMindsAI.py"), encoding="utf-8").read()
    assert 'session.get("consented_sessions")' in src
    assert "session_consent_get" in src


def test_agreeing_is_still_written_to_the_audit_log():
    """The whole reason the duplicate block was safe to delete: THIS is where the
    consent record comes from. If this ever stops, the deletion stops being safe."""
    src = open(os.path.join(ROOT, "TogetherMindsAI.py"), encoding="utf-8").read()
    block = src.split("def _record_consent")[1][:2000]
    assert 'log_event("consent_acknowledged"' in block
    assert "ChatMessage(" in block            # transcript line
    assert "suggestion_cards" in block        # the clinician is told


# ---------------------------------------------------------------------------
# Starting with both on
# ---------------------------------------------------------------------------

def test_the_call_starts_without_waiting_for_a_click():
    """Anchored to the start of a line, not just "the string is present": an
    earlier version of this test passed with `if (false)` in front of the call,
    because it only checked that the text existed somewhere."""
    src = _live_src()
    assert re.search(r"^\s*joinAudio\(\)\.catch\(", src, re.M), \
        "joinAudio must be called as a plain statement, not conditionally"
    # And the button that used to gate it is gone, along with its block.
    assert "rtcJoinBtn" not in src
    assert 'id="rtcConsent"' not in src


def test_mic_and_camera_are_asked_for_in_one_prompt():
    """Two getUserMedia calls mean two permission prompts, one after the other, at
    the start of a therapy session."""
    src = _live_src()
    block = src.split("One mic stream feeds BOTH")[1][:900]
    assert "getUserMedia(" in block
    assert "audio:" in block and "video: _wantVideo" in block


def test_the_camera_reuses_the_track_from_that_prompt():
    """Opening the camera a second time fails outright on some devices, because the
    first handle still holds it."""
    src = _live_src()
    assert "cameraOn(micStream.getVideoTracks()[0]" in src
    assert "new LivekitClient.LocalVideoTrack(existing)" in src


def test_a_camera_that_will_not_start_leaves_the_call_up_on_audio():
    """The clinician and client are mid-session. A failed camera must not take the
    conversation down with it."""
    src = _live_src()
    tail = src.split("if (_wantVideo) {")[1][:400]
    assert "try {" in tail and "catch" in tail
    assert "audio still live" in tail


def test_refusing_both_devices_leaves_an_honest_room():
    """Not a blank pane claiming to be live."""
    src = _live_src()
    block = src.split("Both refused, or no devices")[1][:500]
    assert "setStatus(" in block
    assert "updateMicPill(); updateCamPill();" in block
    assert "return;" in block


def test_a_monitor_room_does_not_ask_for_a_camera_it_never_uses():
    """A monitor watches someone else's camera and publishes none of its own."""
    src = _live_src()
    assert "{% if is_monitor %}false{% else %}true{% endif %}" in src


def test_removing_the_button_did_not_disable_the_whole_rtc_layer():
    """Regression: the entire RTC block was guarded on that button existing
    (`if (!joinBtn) return`). Deleting the button would have silently switched off
    mic, camera, captions and transcription in every session."""
    src = _live_src()
    assert "if (!joinBtn) return;" not in src
    assert 'var livePanel = document.getElementById("rtcLive");' in src
    assert "if (!livePanel) return;" in src


def test_the_live_panel_is_visible_from_the_start():
    """It used to be hidden until the join button was pressed."""
    src = _live_src()
    assert '<div id="rtcLive" class="flex-column flex-grow-1">' in src


def test_leaving_does_not_hide_the_panel_behind_a_block_that_no_longer_exists():
    """leaveAudio used to swap the panel for the consent block. With that gone,
    hiding the panel would leave an empty pane and no way back."""
    src = _live_src()
    block = src.split("function leaveAudio()")[1][:900]
    assert "rtcConsent" not in block
    assert 'document.getElementById("rtcLive").classList.add("d-none")' not in block
    assert "updateMicPill()" in block       # the pills still tell the truth
