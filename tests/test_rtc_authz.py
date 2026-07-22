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
