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
import TogetherMindsAI as tm
from TogetherMindsAI import app, socketio
from models import db, init_encryption, TherapySession, SessionRecording, Clinician
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


# ---------------------------------------------------------------------------
# Retention (Step 3): 30-day lifecycle — stamp on stop, reminder, auto-delete.
# ---------------------------------------------------------------------------

def _seed_recording(sid, status="stopped", expires_in=None, reminded=False,
                    started_by="ther-1", gcs="obj.mp4"):
    now = datetime.now(timezone.utc)
    row = SessionRecording(
        session_id=sid, egress_id="EG", gcs_object=gcs, status=status,
        started_by=started_by, started_at=now, stopped_at=now,
        retention_expires_at=(now + expires_in) if expires_in is not None else None,
        reminder_sent_at=(now if reminded else None),
    )
    db.session.add(row)
    db.session.commit()
    return row.id


def test_stop_stamps_30day_retention(enc_client):
    with app.app_context():
        sid = _seed()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG_1"), \
         patch("recording.stop_recording", return_value=True), \
         patch.object(tm, "_dispatch_recording_ready"):     # don't spawn the email thread
        enc_client.post(f"/session/{sid}/recording/start")
        enc_client.post(f"/session/{sid}/recording/stop")
    with app.app_context():
        row = SessionRecording.query.filter_by(session_id=sid).first()
        assert row.retention_expires_at is not None
        delta = row.retention_expires_at - row.stopped_at
        assert abs(delta - timedelta(days=30)) < timedelta(minutes=1)


def test_sweep_sends_reminder_once(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=12))
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True) as mail, \
         patch("recording.delete_object", return_value=True):
        tm._recording_retention_sweep()
        tm._recording_retention_sweep()                      # idempotent
    assert mail.call_count == 1                              # reminded exactly once
    with app.app_context():
        assert db.session.get(SessionRecording, rid).reminder_sent_at is not None


def test_sweep_deletes_expired_recording_once(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=-1), gcs="gone.mp4")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True), \
         patch("recording.delete_object", return_value=True) as rm:
        tm._recording_retention_sweep()
        tm._recording_retention_sweep()                      # already deleted → no-op
    rm.assert_called_once_with("gone.mp4")
    with app.app_context():
        assert db.session.get(SessionRecording, rid).status == "deleted"


def test_sweep_keeps_object_if_delete_fails(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=-1))
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.delete_object", return_value=False):
        tm._recording_retention_sweep()
    with app.app_context():
        assert db.session.get(SessionRecording, rid).status == "stopped"   # not marked deleted


def _make_therapist_session(client, sid, uid="ther-1"):
    with client.session_transaction() as s:
        s["user_id"] = uid


def test_download_streams_for_therapist(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.download_stream", return_value=(iter([b"abc"]), 3, "video/mp4")):
        rv = enc_client.get(f"/session/{sid}/recording/{rid}/download")
    assert rv.status_code == 200
    assert rv.data == b"abc"
    assert "attachment" in rv.headers.get("Content-Disposition", "")


def test_download_forbidden_for_non_therapist(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    with enc_client.session_transaction() as s:
        s["user_id"] = "intruder"
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get(f"/session/{sid}/recording/{rid}/download").status_code == 403


def test_download_gone_when_deleted(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, status="deleted")
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get(f"/session/{sid}/recording/{rid}/download").status_code == 410


def test_download_404_when_disabled(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", False):
        assert enc_client.get(f"/session/{sid}/recording/{rid}/download").status_code == 404


# ---------------------------------------------------------------------------
# Entitlement gating (Step 4): recording requires the Pro plan once billing is on.
# ---------------------------------------------------------------------------

def _seed_clinician(cid, plan, status="active"):
    db.session.add(Clinician(
        id=cid, provider="google", provider_subject="s-" + cid,
        created_at=datetime.now(timezone.utc), plan=plan, subscription_status=status))
    db.session.commit()


def test_recording_start_requires_premium_when_billing_on(enc_client):
    with app.app_context():
        sid = _seed("doc")
        _seed_clinician("doc", plan="pro")      # has AI analysis but not recording
    with enc_client.session_transaction() as s:
        s["user_id"] = "doc"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(config, "BILLING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG") as start:
        rv = enc_client.post(f"/session/{sid}/recording/start")
    assert rv.status_code == 402
    start.assert_not_called()


def test_recording_start_allowed_for_premium(enc_client):
    with app.app_context():
        sid = _seed("doc")
        _seed_clinician("doc", plan="premium")
    with enc_client.session_transaction() as s:
        s["user_id"] = "doc"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(config, "BILLING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG"), \
         patch("recording.stop_recording", return_value=True), \
         patch.object(tm, "_dispatch_recording_ready"):
        rv = enc_client.post(f"/session/{sid}/recording/start")
    assert rv.status_code == 200 and rv.get_json()["status"] == "active"
