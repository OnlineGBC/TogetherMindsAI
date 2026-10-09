"""
tests/test_rtc_authz.py
-----------------------
Authorization for the realtime-conferencing credential endpoints
(/rtc/livekit-token and /rtc/stt-token). These mint LiveKit room-join JWTs and
AssemblyAI streaming tokens, so knowing a session_id must NOT be enough: only an
admitted participant (the clinician, or a consented + licensure-certified
client) may obtain one — the same gate the live room enforces.

Would have caught the vulnerability where any authenticated caller who knew a
session_id could mint a token and join the audio/transcription stream.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from datetime import datetime, timezone, timedelta

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-rtc")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import config
from TogetherMindsAI import app
from models import db, init_encryption, TherapySession, SessionStateCert
from session_id import generate_session_id
from tests.socket_utils import certify_state

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def enc_client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _seed(therapist="ther-1", mode="couple"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by=therapist, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()
    return sid


def _login(client, user_id, **extra):
    with client.session_transaction() as s:
        s["user_id"] = user_id
        for k, v in extra.items():
            s[k] = v


def _rtc_on():
    """Enable RTC with dummy LiveKit/AssemblyAI credentials for the duration."""
    return patch.multiple(config, RTC_ENABLED=True, LIVEKIT_URL="wss://lk",
                          LIVEKIT_API_KEY="k", LIVEKIT_API_SECRET="s",
                          ASSEMBLYAI_API_KEY="a")


# ---------------------------------------------------------------------------
# LiveKit room-join token
# ---------------------------------------------------------------------------

def test_livekit_token_requires_identity(enc_client):
    sid = _seed()
    with _rtc_on():
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 403 and rv.get_json()["error"] == "no_identity"


def test_livekit_token_forbidden_for_non_participant(enc_client):
    """An authenticated caller who never joined/consented cannot mint a token."""
    sid = _seed()
    _login(enc_client, "stranger")
    with _rtc_on():
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 403 and rv.get_json()["error"] == "not_admitted"


def test_livekit_token_forbidden_for_consented_but_uncertified_client(enc_client):
    """Consent alone is not enough — the licensure gate must also be cleared."""
    sid = _seed()
    _login(enc_client, "cli", consented_sessions=[sid], session_states={sid: "CA"})
    # No SessionStateCert row → clinician has not certified CA → turned away.
    with _rtc_on():
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 403 and rv.get_json()["error"] == "not_admitted"


def test_livekit_token_allowed_for_therapist(enc_client):
    sid = _seed()
    _login(enc_client, "ther-1")
    with _rtc_on():
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 200 and rv.get_json()["token"]


def test_livekit_token_allowed_for_admitted_client(enc_client):
    sid = _seed()
    certify_state(db, SessionStateCert, sid, "ther-1", state="CA")
    _login(enc_client, "cli", consented_sessions=[sid], session_states={sid: "CA"})
    with _rtc_on():
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 200 and rv.get_json()["token"]


def test_livekit_token_503_when_rtc_disabled(enc_client):
    sid = _seed()
    _login(enc_client, "ther-1")
    with patch.object(config, "RTC_ENABLED", False):
        rv = enc_client.post("/rtc/livekit-token", json={"session_id": sid})
    assert rv.status_code == 503


# ---------------------------------------------------------------------------
# One video identity per device (clinician only) + presence by connection
# ---------------------------------------------------------------------------

def _token_identity(client, sid, device_id=None):
    import base64, json
    body = {"session_id": sid}
    if device_id is not None:
        body["device_id"] = device_id
    with _rtc_on():
        rv = client.post("/rtc/livekit-token", json=body)
    assert rv.status_code == 200
    payload = rv.get_json()["token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def test_clinician_two_devices_get_distinct_video_identities(enc_client):
    """Same clinician on two devices must not share an identity (LiveKit would
    kick the first device)."""
    sid = _seed()
    _login(enc_client, "ther-1")
    a = _token_identity(enc_client, sid, "devA")
    b = _token_identity(enc_client, sid, "devB")
    assert a != b
    assert a.startswith("ther-1") and b.startswith("ther-1")


def test_clinician_without_device_id_keeps_plain_identity(enc_client):
    sid = _seed()
    _login(enc_client, "ther-1")
    assert _token_identity(enc_client, sid) == "ther-1"


def test_client_identity_ignores_device_id(enc_client):
    """A patient keeps one identity, so a second device replaces the first."""
    sid = _seed()
    certify_state(db, SessionStateCert, sid, "ther-1", state="CA")
    _login(enc_client, "cli", consented_sessions=[sid], session_states={sid: "CA"})
    assert _token_identity(enc_client, sid, "devA") == "cli"
    assert _token_identity(enc_client, sid, "devB") == "cli"


def test_user_still_connected_until_last_socket_closes():
    import TogetherMindsAI as tm
    with patch.dict(tm.sid_to_user, {"s1": "u", "s2": "u"}, clear=True), \
         patch.dict(tm.sid_to_session, {"s1": "X", "s2": "X"}, clear=True):
        tm.sid_to_user.pop("s1"); tm.sid_to_session.pop("s1")   # first device closes
        assert tm._user_still_connected("u", "X") is True
        tm.sid_to_user.pop("s2"); tm.sid_to_session.pop("s2")   # last device closes
        assert tm._user_still_connected("u", "X") is False


# ---------------------------------------------------------------------------
# AssemblyAI streaming (STT) token
# ---------------------------------------------------------------------------

def test_stt_token_forbidden_for_non_participant(enc_client):
    sid = _seed()
    _login(enc_client, "stranger")
    with _rtc_on(), patch("requests.get") as get:
        rv = enc_client.post("/rtc/stt-token", json={"session_id": sid})
        get.assert_not_called()          # rejected before any provider call
    assert rv.status_code == 403 and rv.get_json()["error"] == "not_admitted"


def test_stt_token_allowed_for_admitted_client(enc_client):
    sid = _seed()
    certify_state(db, SessionStateCert, sid, "ther-1", state="CA")
    _login(enc_client, "cli", consented_sessions=[sid], session_states={sid: "CA"})
    resp = MagicMock()
    resp.json.return_value = {"token": "stt-xyz"}
    resp.raise_for_status.return_value = None
    with _rtc_on(), patch("requests.get", return_value=resp):
        rv = enc_client.post("/rtc/stt-token", json={"session_id": sid})
    assert rv.status_code == 200 and rv.get_json()["token"] == "stt-xyz"
