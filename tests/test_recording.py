"""
tests/test_recording.py
-----------------------
Phase 4 — session recording endpoints (start/stop), behind the RECORDING_ENABLED
flag and therapist-gated. The LiveKit Egress calls are mocked.
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
os.environ.setdefault("SECRET_KEY", "test-secret-recording")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
from TogetherMindsAI import app, socketio
from models import db, init_encryption, TherapySession, SessionRecording
from session_id import generate_session_id

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


def _join(client, sid, uid, mode="solo"):
    sio = socketio.test_client(app, flask_test_client=client)
    sio.emit("join", {"session_id": sid, "user_id": uid, "mode": mode})
    sio.get_received()
    return sio


def test_start_blocked_when_recording_disabled(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", False):
        assert enc_client.post(f"/session/{sid}/recording/start").status_code == 403


def test_start_forbidden_for_non_therapist(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "client-x"
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.post(f"/session/{sid}/recording/start").status_code == 403


def test_start_and_stop_recording(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG_123") as start, \
         patch("recording.stop_recording", return_value=True):
        rv = enc_client.post(f"/session/{sid}/recording/start")
        assert rv.status_code == 200 and rv.get_json()["status"] == "active"
        start.assert_called_once()
        rv2 = enc_client.post(f"/session/{sid}/recording/stop")
        assert rv2.status_code == 200 and rv2.get_json()["stopped"] is True
    with app.app_context():
        rows = SessionRecording.query.filter_by(session_id=sid).all()
        assert len(rows) == 1
        assert rows[0].status == "stopped" and rows[0].egress_id == "EG_123"
        assert rows[0].stopped_at is not None


def test_start_failure_is_recorded(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value=None):
        rv = enc_client.post(f"/session/{sid}/recording/start")
        assert rv.status_code == 502
    with app.app_context():
        assert SessionRecording.query.filter_by(session_id=sid, status="failed").count() == 1


def test_stop_with_no_active_recording(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.post(f"/session/{sid}/recording/stop").status_code == 404


# ---------------------------------------------------------------------------
# Consent state machine (Step 2): records ONLY while every participant consents
# ---------------------------------------------------------------------------

def test_records_only_while_all_consent(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG1") as start, \
         patch("recording.stop_recording", return_value=True) as stop:
        t = _join(enc_client, sid, "ther-1")
        c = _join(enc_client, sid, "client-1")

        t.emit("recording_request", {"session_id": sid, "user_id": "ther-1"})
        assert start.call_count == 0          # client hasn't consented yet → not recording

        c.emit("recording_consent", {"session_id": sid, "user_id": "client-1", "consent": True})
        assert start.call_count == 1          # all consent → recording starts

        c.emit("recording_consent", {"session_id": sid, "user_id": "client-1", "consent": False})
        assert stop.call_count == 1           # one withdrawal → stops

        c.emit("recording_consent", {"session_id": sid, "user_id": "client-1", "consent": True})
        assert start.call_count == 2          # everyone consents again → resumes


def test_request_unavailable_when_disabled(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    with patch.object(config, "RECORDING_ENABLED", False), \
         patch("recording.start_recording", return_value="EG1") as start:
        t = _join(enc_client, sid, "ther-1")
        t.emit("recording_request", {"session_id": sid, "user_id": "ther-1"})
        names = [e["name"] for e in t.get_received()]
        assert "recording_unavailable" in names
        assert start.call_count == 0


def test_request_ignored_from_non_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG1") as start:
        _join(enc_client, sid, "ther-1")
        c = _join(enc_client, sid, "client-1")
        c.emit("recording_request", {"session_id": sid, "user_id": "client-1"})
        assert start.call_count == 0          # a client cannot start recording


def test_unconsented_newcomer_pauses_recording(enc_client):
    with app.app_context():
        sid = _seed("ther-1", mode="group")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG1") as start, \
         patch("recording.stop_recording", return_value=True) as stop:
        t = _join(enc_client, sid, "ther-1", mode="group")
        c1 = _join(enc_client, sid, "client-1", mode="group")
        t.emit("recording_request", {"session_id": sid, "user_id": "ther-1"})
        c1.emit("recording_consent", {"session_id": sid, "user_id": "client-1", "consent": True})
        assert start.call_count == 1          # recording

        _join(enc_client, sid, "client-2", mode="group")   # new, unconsented
        assert stop.call_count == 1           # recording pauses until they consent too
