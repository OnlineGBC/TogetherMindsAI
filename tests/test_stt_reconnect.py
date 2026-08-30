"""
tests/test_stt_reconnect.py
---------------------------
The transcription socket must come back after a network drop.

Reported from two real group sessions (20-27 Aug 2026): a participant dropped,
reconnected, and everything they SAID from then on was missing from the
transcript. Typed chat was fine.

Speech-to-text runs over a second connection, straight from the browser to the
speech service. A drop kills it as well as the chat socket, but only the chat
socket reconnects itself (therapy.js re-emits "join" on connect). The speech
socket had no onclose and no onerror, so nothing noticed it had died: the audio
loop checks readyState and simply returns, binning every frame in silence, while
the pill went on claiming "Transcription on".

Nothing server-side sees any of this — the failing connection never touches our
server — so these read the page source. That is the only place the behaviour
exists, and it is exactly where the bug was.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

ROOM = os.path.join(os.path.dirname(__file__), "..", "templates", "session_live.html")


@pytest.fixture(scope="module")
def src():
    return open(ROOM, encoding="utf-8").read()


def _body_of(src, name):
    """The source of a JS function, by brace matching from its opening line."""
    start = src.index("function " + name + "(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("unbalanced braces in " + name)


# ---------------------------------------------------------------------------
# The handlers that were missing
# ---------------------------------------------------------------------------

def test_the_speech_socket_notices_when_it_closes(src):
    """The whole bug in one line: there was no onclose, so a dead socket looked
    exactly like a working one.

    Matches the HANDLER, not the name. "sttWS.onclose" alone is also satisfied by
    the `= null` in _teardownSTT — checked by deleting the handler and watching
    this test still pass."""
    assert "sttWS.onclose = function" in src


def test_the_speech_socket_notices_an_error(src):
    assert "sttWS.onerror = function" in src


def test_a_drop_schedules_a_reconnect(src):
    body = _body_of(src, "_sttDropped")
    assert "startSTT()" in body
    assert "setTimeout" in body


def test_the_reconnect_backs_off_rather_than_hammering(src):
    """A tight retry loop against a service that is down is worse than waiting."""
    body = _body_of(src, "_sttDropped")
    assert "Math.pow(2" in body
    assert "STT_MAX_RETRIES" in body


def test_it_gives_up_rather_than_retrying_forever(src):
    body = _body_of(src, "_sttDropped")
    assert "sttRetry >= STT_MAX_RETRIES" in body


def test_a_good_connection_clears_the_retry_budget(src):
    """Otherwise the second drop in a long session would start already spent."""
    assert re.search(r"onopen = function[^}]*sttRetry = 0", src, re.S)


# ---------------------------------------------------------------------------
# It must not fight the user
# ---------------------------------------------------------------------------

def test_turning_transcription_off_does_not_reconnect_it(src):
    """stopSTT closes the socket. If that fired the drop handler, switching
    transcription off would switch itself straight back on."""
    body = _body_of(src, "_teardownSTT")
    assert "sttWS.onclose = null" in body
    assert "sttWS.onerror = null" in body
    stop = _body_of(src, "stopSTT")
    assert "clearTimeout(sttRetryTimer)" in stop


def test_a_drop_is_ignored_once_the_user_has_left(src):
    body = _body_of(src, "_sttDropped")
    assert "!sttEnabled || !isJoined()" in body


def test_two_starts_cannot_race(src):
    """A scheduled retry and a user tap can land together."""
    body = _body_of(src, "startSTT")
    assert "sttStarting" in body


def test_a_retry_tears_the_old_audio_graph_down_first(src):
    """Browsers cap how many AudioContexts a page may hold, and the old graph
    would go on feeding a dead socket."""
    body = _body_of(src, "_startSTT")
    assert body.index("_teardownSTT()") < body.index("new (window.AudioContext")


def test_a_refused_token_is_treated_as_a_drop(src):
    """It used to return quietly, leaving transcription off with the pill on."""
    body = _body_of(src, "_startSTT")
    assert re.search(r"if \(!tr\.ok\) \{ _sttDropped\(\);", body)


# ---------------------------------------------------------------------------
# The pill must not lie
# ---------------------------------------------------------------------------

def test_the_pill_says_reconnecting_rather_than_on(src):
    """Claiming "Transcription on" over a dead socket is how a whole reconnect's
    worth of speech went missing without anyone noticing."""
    body = _body_of(src, "setSttIndicator")
    assert "sttReconnecting" in body
    assert "reconnecting" in body.lower()


def test_giving_up_leaves_the_pill_off_not_on(src):
    body = _body_of(src, "_sttDropped")
    assert "sttEnabled = false" in body
    assert "setSttIndicator(false)" in body


# ---------------------------------------------------------------------------
# Mic recovery
# ---------------------------------------------------------------------------

def test_mic_recovery_restarts_a_dead_speech_socket(src):
    """Whatever interrupted the mic — an app-switch, a screen lock, a network
    drop — usually took the speech socket too. Re-pointing audio at a dead socket
    just bins it quietly."""
    body = _body_of(src, "recoverMic")
    assert "sttWS.readyState === 1" in body
    assert "startSTT()" in body
