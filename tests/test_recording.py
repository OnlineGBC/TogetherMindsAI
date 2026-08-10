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
from models import db, init_encryption, TherapySession, SessionRecording, Clinician, SessionStateCert
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


def _join(client, sid, uid, mode="solo"):
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
                    early_reminded=False, started_by="ther-1", gcs="obj.mp4",
                    token="dltok"):
    now = datetime.now(timezone.utc)
    # `reminded` is either a bool (final notice sent just now) or a timedelta saying
    # how long AGO it was sent — deletion only happens once that notice has aged.
    if isinstance(reminded, timedelta):
        reminded_at = now - reminded
    else:
        reminded_at = now if reminded else None
    row = SessionRecording(
        session_id=sid, egress_id="EG", gcs_object=gcs, status=status,
        started_by=started_by, started_at=now, stopped_at=now,
        retention_expires_at=(now + expires_in) if expires_in is not None else None,
        reminder_sent_at=reminded_at,
        early_reminder_sent_at=(now if early_reminded else None),
        download_token=token,
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
    # early_reminded: the 7-day warning already went out, so only the final notice is due.
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=12), early_reminded=True)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True) as mail, \
         patch("recording.delete_object", return_value=True):
        tm._recording_retention_sweep()
        tm._recording_retention_sweep()                      # idempotent
    assert mail.call_count == 1                              # reminded exactly once
    with app.app_context():
        assert db.session.get(SessionRecording, rid).reminder_sent_at is not None


def test_sweep_deletes_expired_recording_once(enc_client):
    # Deletion now requires an aged final notice — seed one sent 48h ago.
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=-1), gcs="gone.mp4",
                              reminded=timedelta(hours=48), early_reminded=True)
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
        rid = _seed_recording(sid, expires_in=timedelta(hours=-1),
                              reminded=timedelta(hours=48), early_reminded=True)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.delete_object", return_value=False):
        tm._recording_retention_sweep()
    with app.app_context():
        assert db.session.get(SessionRecording, rid).status == "stopped"   # not marked deleted


def _sweep_email_kinds(mail):
    """The email kinds the sweep sent, in order — _email_recording(row, kind)."""
    return [c.args[1] for c in mail.call_args_list]


def test_sweep_sends_early_warning_once(enc_client):
    """A recording a week out gets the early warning and nothing else."""
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(days=5))
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True) as mail, \
         patch("recording.delete_object", return_value=True) as rm:
        tm._recording_retention_sweep()
        tm._recording_retention_sweep()                      # idempotent
    assert _sweep_email_kinds(mail) == ["early"]             # no final notice yet
    rm.assert_not_called()
    with app.app_context():
        row = db.session.get(SessionRecording, rid)
        assert row.early_reminder_sent_at is not None
        assert row.reminder_sent_at is None


def test_sweep_sends_both_warnings_when_deadline_is_close(enc_client):
    """Inside 48h with no early warning yet (e.g. a short retention), both go out —
    early first, then the final notice."""
    with app.app_context():
        sid = _seed()
        _seed_recording(sid, expires_in=timedelta(hours=30))
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True) as mail, \
         patch("recording.delete_object", return_value=True):
        tm._recording_retention_sweep()
    assert _sweep_email_kinds(mail) == ["early", "reminder"]


def test_expired_but_unwarned_recording_is_warned_not_deleted(enc_client):
    """The bug this fixes: a deadline that slipped past between sweeps must get its
    final notice, NOT a silent deletion."""
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(hours=-1), early_reminded=True)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True) as mail, \
         patch("recording.delete_object", return_value=True) as rm:
        tm._recording_retention_sweep()
    assert _sweep_email_kinds(mail) == ["reminder"]           # warned, despite being late
    rm.assert_not_called()                                    # and NOT deleted in the same run
    with app.app_context():
        assert db.session.get(SessionRecording, rid).status == "stopped"


def test_expired_recording_waits_for_the_warning_to_age(enc_client):
    """Deletion holds off until the final notice is old enough to have been acted on."""
    with app.app_context():
        sid = _seed()
        fresh = _seed_recording(sid, expires_in=timedelta(hours=-1), token="tokFresh",
                                reminded=timedelta(hours=1), early_reminded=True)
        aged  = _seed_recording(sid, expires_in=timedelta(hours=-1), token="tokAged",
                                gcs="aged.mp4", reminded=timedelta(hours=25),
                                early_reminded=True)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=True), \
         patch("recording.delete_object", return_value=True) as rm:
        tm._recording_retention_sweep()
    rm.assert_called_once_with("aged.mp4")                    # only the aged one goes
    with app.app_context():
        assert db.session.get(SessionRecording, fresh).status == "stopped"
        assert db.session.get(SessionRecording, aged).status == "deleted"


def test_backstop_deletes_recording_that_could_never_be_warned(enc_client):
    """If no warning is deliverable (no clinician email, SMTP down), we still do not
    retain session video indefinitely — the backstop deletes and logs why."""
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, expires_in=timedelta(days=-8), gcs="stale.mp4")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_email_recording", return_value=False), \
         patch.object(tm, "log_event") as logged, \
         patch("recording.delete_object", return_value=True) as rm:
        tm._recording_retention_sweep()
    rm.assert_called_once_with("stale.mp4")
    with app.app_context():
        row = db.session.get(SessionRecording, rid)
        assert row.status == "deleted"
        assert row.reminder_sent_at is None                   # never successfully warned
    assert logged.call_args.kwargs["trigger"] == "retention_backstop"


def _make_therapist_session(client, sid, uid="ther-1"):
    with client.session_transaction() as s:
        s["user_id"] = uid


def test_download_redirects_to_signed_url_for_therapist(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.signed_download_url",
               return_value="https://storage.example/signed?x=1") as signer:
        rv = enc_client.get("/recording/download/dltok")
    assert rv.status_code in (302, 303)                         # redirect to GCS
    assert rv.headers["Location"] == "https://storage.example/signed?x=1"
    signer.assert_called_once()


def test_download_redirects_to_login_when_not_signed_in(enc_client):
    # Email link opened on a phone with no sign-in → bounce to sign-in, not 403.
    with app.app_context():
        sid = _seed(); _seed_recording(sid)
    with patch.object(config, "RECORDING_ENABLED", True):
        rv = enc_client.get("/recording/download/dltok")
    assert rv.status_code == 302
    loc = rv.headers["Location"]
    assert "/login" in loc and "next=" in loc


def test_download_forbidden_for_non_therapist(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    with enc_client.session_transaction() as s:
        s["user_id"] = "intruder"
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get("/recording/download/dltok").status_code == 403


def test_download_gone_when_deleted(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid, status="deleted")
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get("/recording/download/dltok").status_code == 410


def test_download_404_when_disabled(enc_client):
    with app.app_context():
        sid = _seed()
        rid = _seed_recording(sid)
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", False):
        assert enc_client.get("/recording/download/dltok").status_code == 404


def test_download_404_for_unknown_token(enc_client):
    with app.app_context():
        sid = _seed()
        _seed_recording(sid)
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get("/recording/download/no-such-token").status_code == 404


def test_download_url_has_no_session_id(enc_client):
    """Security regression: the emailed download URL must be keyed on the opaque
    token, never the session id."""
    with app.app_context():
        sid = _seed()
        _seed_recording(sid, token="secrettoken")
        row = SessionRecording.query.filter_by(session_id=sid).first()
        url = tm._recording_download_url(row)
    assert "secrettoken" in url
    assert sid not in url
    assert "/recording/download/" in url


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


# ---------------------------------------------------------------------------
# Token-keyed transcript docs for the recording email (Batch C): video + PDF + Word.
# ---------------------------------------------------------------------------

import io as _io


def test_recording_doc_pdf_for_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="dltok")
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_transcript_pdf_buf", return_value=_io.BytesIO(b"%PDF-1.4 x")):
        rv = enc_client.get("/recording/download/dltok/pdf")
    assert rv.status_code == 200
    assert "attachment" in rv.headers.get("Content-Disposition", "")


def test_recording_doc_docx_for_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="dltok")
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(tm, "_transcript_docx_buf", return_value=_io.BytesIO(b"PK x")):
        rv = enc_client.get("/recording/download/dltok/docx")
    assert rv.status_code == 200


def test_recording_doc_forbidden_for_non_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="dltok")
    with enc_client.session_transaction() as s:
        s["user_id"] = "intruder"
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get("/recording/download/dltok/pdf").status_code == 403


def test_recording_doc_404_bad_token_or_format(enc_client):
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="dltok")
    _make_therapist_session(enc_client, sid)
    with patch.object(config, "RECORDING_ENABLED", True):
        assert enc_client.get("/recording/download/nope/pdf").status_code == 404
        assert enc_client.get("/recording/download/dltok/txt").status_code == 404


def test_start_recording_pins_720p_encoding():
    """The egress request must set encoding explicitly. LiveKit's default preset is
    1080p at ~3-4 Mbps, which produced roughly 1 GB for a 45-minute session."""
    import recording as rec
    with patch("recording.requests.post") as post, \
         patch("recording._egress_jwt", return_value="tok"), \
         patch.object(config, "LIVEKIT_URL", "wss://rtc.example.com"), \
         patch.object(config, "RECORDINGS_BUCKET", "bkt"):
        post.return_value.json.return_value = {"egressId": "EG_X"}
        post.return_value.raise_for_status.return_value = None
        egress_id = rec.start_recording("room-1", "sess/a.mp4")

    assert egress_id == "EG_X"
    adv = post.call_args.kwargs["json"]["advanced"]
    assert (adv["width"], adv["height"]) == (1280, 720)
    assert adv["framerate"] == 30
    assert adv["video_bitrate"] == 1200
    assert adv["audio_bitrate"] == 96
    # Codecs are deliberately left unset so LiveKit picks MP4-appropriate ones.
    assert "video_codec" not in adv and "audio_codec" not in adv


def test_recording_email_has_three_token_links(enc_client):
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="tok9")
        row = SessionRecording.query.filter_by(session_id=sid).first()
        subject, plain, html = tm._recording_email_content(row, "ready")
    for body in (plain, html):
        assert "/recording/download/tok9" in body          # video
        assert "/recording/download/tok9/pdf" in body      # PDF
        assert "/recording/download/tok9/docx" in body     # Word
    assert sid not in plain.split("Session:")[0]           # no session id in the links


def test_recording_email_wording_and_retention_bullets(enc_client):
    """Both the 'ready' and 'reminder' emails use audio/video wording and carry the
    retention/download guidance bullets (video expires; transcript kept 6 years)."""
    with app.app_context():
        sid = _seed("ther-1"); _seed_recording(sid, token="tokA")
        row = SessionRecording.query.filter_by(session_id=sid).first()
        for kind in ("ready", "reminder"):
            subject, plain, html = tm._recording_email_content(row, kind)
            assert "audio/video" in subject                       # renamed from "recording"
            for body in (plain, html):
                assert "download both the video and the transcript" in body
                assert "retained for six years" in body
                assert "prevailing US law" in body
                assert "cannot be recovered" in body
            assert "<ul" in html and "<li>" in html               # rendered as bullets


def _email_at(rid, kind, expires_utc):
    """Render an email for a recording with an exact UTC deadline."""
    with app.app_context():
        row = db.session.get(SessionRecording, rid)
        row.retention_expires_at = expires_utc
        db.session.commit()
        return tm._recording_email_content(row, kind)


def test_reminder_email_names_the_date_not_tomorrow(enc_client):
    """The sweep reminds anything expiring within the next 24h, so the deadline is
    often the SAME calendar day as the email. It must state the dated deadline —
    never the word 'tomorrow'."""
    with app.app_context():
        sid = _seed("ther-1")
        rid = _seed_recording(sid, expires_in=timedelta(hours=12), token="tokR")
    # 16:00 UTC is midday in Eastern — same calendar date either way, so this test
    # stays about the wording. The conversion itself is covered separately below.
    subject, plain, html = _email_at(rid, "reminder",
                                     datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
    assert "09 Aug 2026" in subject                        # dated deadline
    for body in (subject, plain, html):
        assert "tomorrow" not in body.lower()
        assert "09 Aug 2026" in body
    # The heading is built explicitly rather than str.capitalize()'d off the subject —
    # capitalize() would lowercase the month into "09 aug 2026".
    assert "Final notice: your audio/video session will be deleted on 09 Aug 2026" in html


def test_emails_carry_no_utc_label_and_use_eastern_dates(enc_client):
    """Dates are shown in US Eastern with no zone label. The stored deadline is UTC,
    so it must be CONVERTED — 09 Aug 02:00 UTC is still the 8th in Eastern, and an
    unconverted date would name a day the reader has not reached."""
    with app.app_context():
        sid = _seed("ther-1")
        rid = _seed_recording(sid, expires_in=timedelta(days=3), token="tokZ")
    for kind in ("ready", "early", "reminder"):
        subject, plain, html = _email_at(rid, kind,
                                         datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc))
        for body in (subject, plain, html):
            assert "UTC" not in body                       # no zone label anywhere
        for body in (plain, html):
            # Everything above the footer — the footer stamps TODAY's date, which
            # would otherwise collide with the deadline assertions below.
            above_footer = body.split("Sent automatically")[0]
            assert "08 Aug 2026" in above_footer           # converted to Eastern
            assert "09 Aug 2026" not in above_footer       # NOT the raw UTC date


def test_emails_say_audio_video_session_and_drop_relative_days(enc_client):
    """Wording: 'audio/video session', and no 'about N days from now' countdown."""
    with app.app_context():
        sid = _seed("ther-1")
        rid = _seed_recording(sid, expires_in=timedelta(days=7), token="tokW")
    for kind in ("ready", "early", "reminder"):
        subject, plain, html = _email_at(rid, kind,
                                         datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
        assert "audio/video session" in subject
        assert "session audio/video" not in subject
        for body in (plain, html):
            assert "session audio/video" not in body
            assert "days from now" not in body


def test_emails_footer_stamps_send_time_in_us_eastern(enc_client):
    """Footer says when the email was sent, in US Eastern, in both parts."""
    import re
    with app.app_context():
        sid = _seed("ther-1")
        rid = _seed_recording(sid, expires_in=timedelta(days=7), token="tokT")
    _, plain, html = _email_at(rid, "early",
                               datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
    stamp = re.compile(r"Sent automatically by TogetherMindsAI at "
                       r"\d{2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d{2} [AP]M US Eastern time\.")
    for body in (plain, html):
        assert stamp.search(body), f"footer stamp missing/misformatted in: {body[-300:]}"
        assert "Sent automatically by TogetherMindsAI." not in body   # old bare footer


def test_recording_email_bullets_are_parallel_and_flag_dead_links(enc_client):
    """Bullet 3 joins the duration and the legal condition in parallel, and the
    email says the links stop working once the recording is deleted."""
    with app.app_context():
        sid = _seed("ther-1")
        _seed_recording(sid, expires_in=timedelta(days=30), token="tokB")
        row = SessionRecording.query.filter_by(session_id=sid).first()
        for kind in ("ready", "reminder"):
            _, plain, html = tm._recording_email_content(row, kind)
            for body in (plain, html):
                assert ("retained for six years, or longer if prevailing US law "
                        "requires it") in body
                assert "as the clinical record." not in body   # old trailing modifier
                assert "download links below will stop working" in body
            # "audio/video" survives in the subject line and the opening sentence only.
            assert html.count("audio/video") <= 2


def test_send_email_from_header_has_product_display_name():
    """Clinician-facing mail should read as the product, not as personal mail from
    whoever happens to own the SMTP account."""
    with patch("TogetherMindsAI.smtplib") as mock_smtplib, \
         patch.object(config, "FEEDBACK_FROM_EMAIL", "noreply@example.com"), \
         patch.object(config, "FEEDBACK_SMTP_HOST", "smtp.example.com"), \
         patch.object(config, "FEEDBACK_SMTP_PORT", 587), \
         patch.object(config, "FEEDBACK_SMTP_USER", "u"), \
         patch.object(config, "FEEDBACK_SMTP_PASSWORD", "p"):
        smtp = mock_smtplib.SMTP.return_value.__enter__.return_value
        tm._send_email(["doc@example.com"], "Subj", "plain", "<p>html</p>")
    msg = smtp.send_message.call_args[0][0]
    assert msg["From"] == "TogetherMindsAI <noreply@example.com>"
    assert msg["To"] == "doc@example.com"


def test_recording_email_shows_friendly_name_and_access_note(enc_client):
    """The email shows the friendly session name alongside the ID, and a prominent
    note that only the signed-in clinician can download."""
    with app.app_context():
        sid = _seed("ther-1")
        ts = db.session.get(TherapySession, sid)
        ts.friendly_name = "Smith weekly"
        db.session.commit()
        _seed_recording(sid, token="tokF")
        row = SessionRecording.query.filter_by(session_id=sid).first()
        _, plain, html = tm._recording_email_content(row, "ready")
    for body in (plain, html):
        assert "Smith weekly" in body                    # friendly name shown
        assert sid in body                               # ID still shown alongside
        assert "No one else can download" in body        # prominent access-control note


def test_end_session_stops_active_recording_then_emails_recording(enc_client):
    # Ending a session (over HTTP) with a recording running stops egress and emails
    # the RECORDING (once, on end), not the transcript-only email.
    with app.app_context():
        sid = _seed("ther-1")
        rid = _seed_recording(sid, status="active")
    _join(enc_client, sid, "ther-1")
    tm.session_recording_requested[sid] = True
    tm.session_recording_active[sid] = rid
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.stop_recording", return_value=True) as stop, \
         patch.object(tm, "_dispatch_recording_ready") as recording_email, \
         patch.object(tm, "_dispatch_session_transcript") as transcript:
        rv = enc_client.post(f"/session/{sid}/end")
    assert rv.status_code in (302, 303)            # form post → redirect
    assert stop.call_count == 1
    recording_email.assert_called_once_with(rid)   # emailed the recording
    transcript.assert_not_called()
    assert tm.session_recording_active.get(sid) is None


def test_recording_email_not_sent_on_consent_pause(enc_client):
    # A newcomer who hasn't consented pauses recording — but that must NOT email
    # (emails are sent only when the session ends).
    with app.app_context():
        sid = _seed("ther-1", mode="group")
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG1"), \
         patch("recording.stop_recording", return_value=True), \
         patch.object(tm, "_dispatch_recording_ready") as recording_email:
        t  = _join(enc_client, sid, "ther-1", mode="group")
        c1 = _join(enc_client, sid, "client-1", mode="group")
        t.emit("recording_request", {"session_id": sid, "user_id": "ther-1"})
        c1.emit("recording_consent", {"session_id": sid, "user_id": "client-1", "consent": True})
        _join(enc_client, sid, "client-2", mode="group")   # unconsented newcomer → pause
    recording_email.assert_not_called()
