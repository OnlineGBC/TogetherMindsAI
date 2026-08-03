"""
tests/test_transcription_toggle.py
----------------------------------
Live transcription is a per-session feature that DEFAULTS OFF and is controlled
only by the session's clinician (a therapist-only toggle). Clients follow via the
`transcription_state` socket event so their UI and consent copy always match
whether transcription can occur.

These tests would have caught: transcription running by default, a non-therapist
turning it on, or the master TRANSCRIPTION_ENABLED kill-switch being ignored.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from datetime import datetime, timezone, timedelta

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-transcription")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import config
import TogetherMindsAI as tm
from TogetherMindsAI import app, socketio
from models import db, init_encryption, TherapySession, SessionStateCert
from session_id import generate_session_id
from tests.socket_utils import authed_socket, certify_state

init_encryption(TEST_KEY)


@pytest.fixture
def enc_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _seed(therapist="ther-1", mode="solo"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by=therapist, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()
    return sid


def _join(sid, uid, mode="solo"):
    ts = db.session.get(TherapySession, sid)
    ther = ts.therapist_id if ts else None
    if uid == ther:
        sio = authed_socket(app, socketio, uid, clinician=True)
    else:
        if ther:
            certify_state(db, SessionStateCert, sid, ther, state="CA")
        sio = authed_socket(app, socketio, uid, session_id=sid, state="CA")
    sio.emit("join", {"session_id": sid, "mode": mode})
    sio.get_received()
    return sio


def _states(sio):
    return [e["args"][0] for e in sio.get_received() if e["name"] == "transcription_state"]


ON = lambda: patch.object(config, "TRANSCRIPTION_ENABLED", True)
RTC = lambda: patch.object(config, "RTC_ENABLED", True)


def test_join_syncs_default_off(enc_client):
    """A newcomer is told transcription is off (the default) on join."""
    with app.app_context():
        sid = _seed()
        with ON(), RTC():
            _join(sid, "ther-1")
            certify_state(db, SessionStateCert, sid, "ther-1", state="CA")
            c = authed_socket(app, socketio, "client-1", session_id=sid, state="CA")
            c.emit("join", {"session_id": sid, "mode": "solo"})
            states = _states(c)
        assert states and states[-1]["on"] is False
        assert not tm.session_transcription_on.get(sid)


def test_therapist_toggle_on_broadcasts_to_all(enc_client):
    with app.app_context():
        sid = _seed()
        with ON(), RTC():
            t = _join(sid, "ther-1")
            c = _join(sid, "client-1")
            c.get_received()
            t.emit("set_transcription", {"session_id": sid, "on": True})
            tstates, cstates = _states(t), _states(c)
        assert tstates and tstates[-1]["on"] is True
        assert cstates and cstates[-1]["on"] is True
        assert tm.session_transcription_on.get(sid) is True


def test_therapist_can_toggle_back_off(enc_client):
    with app.app_context():
        sid = _seed()
        with ON(), RTC():
            t = _join(sid, "ther-1")
            t.emit("set_transcription", {"session_id": sid, "on": True})
            t.emit("set_transcription", {"session_id": sid, "on": False})
            states = _states(t)
        assert states and states[-1]["on"] is False
        assert tm.session_transcription_on.get(sid) is False


def test_non_therapist_cannot_toggle(enc_client):
    with app.app_context():
        sid = _seed()
        with ON(), RTC():
            _join(sid, "ther-1")
            c = _join(sid, "client-1")
            c.emit("set_transcription", {"session_id": sid, "on": True})
        assert not tm.session_transcription_on.get(sid)   # stays off


def test_master_flag_off_is_noop(enc_client):
    with app.app_context():
        sid = _seed()
        with patch.object(config, "TRANSCRIPTION_ENABLED", False), RTC():
            t = _join(sid, "ther-1")
            t.emit("set_transcription", {"session_id": sid, "on": True})
        assert not tm.session_transcription_on.get(sid)


def test_consent_gate_copy_reflects_flag(enc_client):
    with app.app_context():
        sid = _seed(therapist="ther-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "client-9"          # not the therapist, not yet consented
    with ON():
        html = enc_client.get(f"/session/{sid}/consent").get_data(as_text=True)
        assert "off unless your clinician turns it on" in html
        assert "consent to live AI transcription" in html
    with patch.object(config, "TRANSCRIPTION_ENABLED", False):
        html = enc_client.get(f"/session/{sid}/consent").get_data(as_text=True)
        assert "This is a text session with your clinician" in html
        assert "consent to live AI transcription" not in html


def test_master_flag_defaults_available():
    """The feature ships available (env unset) so the therapist toggle is usable;
    per-session state still defaults off."""
    assert config.TRANSCRIPTION_ENABLED is True
