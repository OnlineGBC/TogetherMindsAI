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


# ---------------------------------------------------------------------------
# A monitor room drops what it cannot use
# ---------------------------------------------------------------------------

def test_the_caregiver_room_has_no_copilot(client):
    """A monitor room has no words for the co-pilot to read, so the panel could
    only ever sit there empty. The script is not even fetched."""
    html = _room(client, roles.CAREGIVER)
    assert "therapist-console.js" not in html
    assert "initTherapistConsole" not in html


def test_other_roles_still_get_the_copilot(client):
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        html = _room(client, role)
        assert "therapist-console.js" in html, role
        assert "initTherapistConsole" in html, role


def test_the_caregiver_room_still_sends_the_presence_heartbeat(client):
    """The co-pilot gate sits INSIDE the therapist block; it must not take the
    heartbeat with it. Without the heartbeat, clients are never admitted."""
    html = _room(client, roles.CAREGIVER)
    assert "/heartbeat" in html


def test_the_caregiver_room_has_both_full_screen_and_shrink(client):
    """Everyone in a monitor watches the same camera, so full screen is the useful
    control. Shrink stays too: a parent or nurse who joins with their camera on has
    a self-view to move out of the way, exactly like anyone else. The button hides
    itself when the camera is off, so nobody sees a control with nothing to do."""
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcFullscreenBtn"' in html
    assert 'id="rtcSelfMiniBtn"' in html


def test_other_roles_keep_shrink_and_have_no_full_screen(client):
    html = _room(client, roles.PSYCHOTHERAPIST)
    assert 'id="rtcSelfMiniBtn"' in html
    assert 'id="rtcFullscreenBtn"' not in html


def test_shrink_survives_full_screen():
    """The full-screen rules size EVERY tile to fill, at the same specificity as the
    shrink rule and later in the file — so without this the shrunken self-view would
    be blown back up the moment you went full screen."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    assert '#rtcLive:fullscreen #rtcVideoGrid.self-mini [data-tile="local"]' in css
    assert '#rtcLive.fill-window #rtcVideoGrid.self-mini [data-tile="local"]' in css
    # And it must come AFTER the rules it has to beat.
    assert css.index("#rtcLive:fullscreen #rtcVideoGrid.self-mini") > css.index("#rtcLive:fullscreen .rtc-tile")


def test_the_caregiver_room_has_no_background_chooser(client):
    """You are watching someone else's camera; there is no own background."""
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcBgSelect"' not in html
    assert 'id="rtcBgUpload"' not in html


def test_other_roles_keep_the_background_chooser(client):
    html = _room(client, roles.HYPNOTHERAPIST)
    assert 'id="rtcBgSelect"' in html


def test_full_screen_falls_back_to_filling_the_window(client):
    """iPhone Safari will not fullscreen a container. The fallback must be our own
    CSS cover, NOT video.webkitEnterFullscreen — the iOS native player drops the
    Brighten filter, and brightening is the point of a monitor in a dim room."""
    html = _room(client, roles.CAREGIVER)
    assert "fill-window" in html
    # The CALL, not the word — the comment above it explains why we avoid it.
    assert ".webkitEnterFullscreen(" not in html


def test_the_fill_window_cover_sits_below_bootstrap_modals():
    """A consent or licensure prompt must never open behind the video."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    block = css.split("#rtcLive.fill-window {")[1].split("}")[0]
    z = int([l for l in block.splitlines() if "z-index" in l][0]
            .split(":")[1].strip().rstrip(";"))
    assert z < 1050, z          # Bootstrap's modal backdrop


def test_the_monitor_keeps_the_screen_awake(client):
    """A phone propped up watching a crib sleeps in a minute and shows nothing."""
    html = _room(client, roles.CAREGIVER)
    assert "wakeLock" in html
    assert 'navigator.wakeLock.request("screen")' in html


def test_other_roles_do_not_take_a_wake_lock(client):
    """Only the room whose job is watching. A therapy session holds attention by
    itself, and a needless wake lock costs battery."""
    html = _room(client, roles.PSYCHOTHERAPIST)
    assert "wakeLock" not in html


def test_full_screen_also_enlarges_the_tile_not_just_the_container(client):
    """Regression: the grid went full screen but the tile kept aspect-ratio 4/3,
    max-height 72vh and the phone width clamp, so a full screen showed a small
    tile in the corner of a black screen. The browser enlarges the ELEMENT, never
    its children — real fullscreen needs the same overrides as the fallback."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    # Both paths must reset the tile, not just the fallback.
    # The PANE is what goes full screen, not the grid — fullscreen shows only the
    # element it was given, and the controls live outside the grid.
    assert "#rtcLive:fullscreen .rtc-tile" in css
    assert "#rtcLive.fill-window .rtc-tile" in css
    tile_rules = css.split("#rtcLive.fill-window .rtc-tile,")[1].split("}")[0]
    for prop in ("aspect-ratio: auto", "max-height: none", "height: 100%"):
        assert prop in tile_rules, prop


def test_the_webkit_fullscreen_selector_is_its_own_rule():
    """A browser that does not know :-webkit-full-screen discards the whole rule it
    sits in — grouping it with :fullscreen would take the standard one down too."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    # Selector lines only — the comment above them names both on purpose.
    for line in css.splitlines():
        if "#rtcLive:-webkit-full-screen" in line:
            assert "#rtcLive:fullscreen" not in line, line.strip()


def test_the_monitor_shows_the_whole_frame_not_a_crop(client):
    """The tile sets object-fit:cover inline. Cropping can put the very thing being
    watched outside the frame, so full screen shows the whole picture."""
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    assert "object-fit: contain !important" in css


# ---------------------------------------------------------------------------
# What full screen looked like on a real phone
# ---------------------------------------------------------------------------

def test_full_screen_takes_the_pane_so_the_controls_stay_reachable(client):
    """Fullscreen shows ONLY the element it is given. Handing it the grid hid Exit
    and Brighten, leaving the phone's own "drag from the top" as the only way out
    — and no way at all to brighten a dim room while watching it."""
    html = _room(client, roles.CAREGIVER)
    assert 'var pane = document.getElementById("rtcLive")' in html
    assert 'pane.requestFullscreen' in html


def test_full_screen_offers_fill_as_well_as_fit(client):
    """A wide picture on a tall phone is mostly black bars. Fit shows the whole
    frame, Fill crops to use the screen. Fit stays the default — cropping can put
    the very thing being watched out of frame."""
    html = _room(client, roles.CAREGIVER)
    assert 'id="rtcFitFillBtn"' in html
    assert "video-fill" in html
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    assert "object-fit: contain !important" in css      # the default
    assert "object-fit: cover !important" in css        # opted into by Fill


def test_full_screen_asks_for_landscape(client):
    html = _room(client, roles.CAREGIVER)
    assert 'screen.orientation.lock("landscape")' in html
    assert "screen.orientation.unlock()" in html


def test_full_screen_darkens_the_status_bar(client):
    """Android paints the status bar from theme-color, which showed as a teal
    stripe above a full-screen video."""
    html = _room(client, roles.CAREGIVER)
    assert 'meta[name="theme-color"]' in html
    assert '"#000000"' in html


def test_the_tile_label_is_styled_from_css_not_inline(client):
    """An inline style cannot be overridden without !important, and at bottom:2px
    the label sat under the phone's gesture bar in full screen."""
    html = _room(client, roles.CAREGIVER)
    assert "position:absolute;left:4px;bottom:2px" not in html
    css = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", "css", "style.css"), encoding="utf-8").read()
    assert ".rtc-tile-label {" in css
    assert "#rtcLive:fullscreen .rtc-tile-label" in css


# ---------------------------------------------------------------------------
# The person leading the session is named for what they ARE
# ---------------------------------------------------------------------------

def _default_name_for(role):
    """The name the session leader is given on joining a room led by this role."""
    import TogetherMindsAI as tm
    from tests.socket_utils import authed_socket
    with app.app_context():
        db.session.query(Clinician).delete()
        db.session.query(TherapySession).delete()
        now = datetime.now(timezone.utc)
        db.session.add(Clinician(id="doc", provider="google", provider_subject="doc",
                                 email="doc@example.com", role=role, created_at=now))
        db.session.add(TherapySession(
            id="s2", mode="solo", created_by="doc", created_at=now,
            retention_expires_at=now + timedelta(days=30), therapist_id="doc"))
        db.session.commit()
    sio = authed_socket(app, tm.socketio, "doc", clinician=True)
    sio.emit("join", {"session_id": "s2", "mode": "solo"})
    for event in sio.get_received():
        if event["name"] == "history":
            return event["args"][0]["default_name"]
    return None


def test_a_caregiver_is_not_labelled_therapist(client):
    """A caregiver watching a baby was labelled "Therapist" on the video tile."""
    assert _default_name_for(roles.CAREGIVER) == "Caregiver"


def test_the_clinical_roles_keep_their_own_words(client):
    """Taken from the role's own wording table, so a role added later is named
    correctly without touching the join handler."""
    assert _default_name_for(roles.PSYCHOTHERAPIST) == "Clinician"
    assert _default_name_for(roles.HYPNOTHERAPIST) == "Practitioner"
