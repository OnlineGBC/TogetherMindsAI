"""
tests/test_session_controls.py
------------------------------
Batch A session controls: 'Hide my data' is client-only; only the clinician can
end a session (clients are notified); only the clinician can set the shared
friendly name (clients get it, late joiners are synced).
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
os.environ.setdefault("SECRET_KEY", "test-secret-sc")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from datetime import datetime, timezone, timedelta

import TogetherMindsAI as tm
from TogetherMindsAI import app, socketio
from models import db, init_encryption, TherapySession
from session_id import generate_session_id

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


def _seed(therapist="ther-1", mode="group"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(id=sid, mode=mode, created_by=therapist, created_at=now,
                                  retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()
    return sid


def _join(client, sid, uid):
    sio = socketio.test_client(app, flask_test_client=client)
    sio.emit("join", {"session_id": sid, "user_id": uid, "mode": "group"})
    sio.get_received()
    return sio


def _names(sio):
    return [e["name"] for e in sio.get_received()]


# ---- Hide my data (progress page) ----

def test_hide_my_data_hidden_for_clinician(enc_client):
    with enc_client.session_transaction() as s:
        s["user_id"] = "doc"; s["clinician_id"] = "doc"
    rv = enc_client.get("/progress/doc/couple")
    assert rv.status_code == 200
    assert b"Hide my data" not in rv.data


def test_hide_my_data_shown_for_client(enc_client):
    with enc_client.session_transaction() as s:
        s["user_id"] = "cli"
    rv = enc_client.get("/progress/cli/couple")
    assert rv.status_code == 200
    assert b"Hide my data" in rv.data


# ---- End session ----

def test_therapist_end_session_notifies_clients(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    t.emit("end_session", {"session_id": sid, "user_id": "ther-1"})
    assert "session_ended" in _names(c)        # client is notified
    assert "session_ended" not in _names(t)    # not echoed to the ender


def test_non_therapist_cannot_end_session(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    c.emit("end_session", {"session_id": sid, "user_id": "client-1"})
    assert "session_ended" not in _names(t)


# ---- Friendly name ----

def test_therapist_sets_friendly_name_broadcasts_and_stores(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    t.emit("set_friendly_name", {"session_id": sid, "user_id": "ther-1", "name": "Smith — wk3"})
    fn = [e for e in c.get_received() if e["name"] == "friendly_name_set"]
    assert fn and fn[0]["args"][0]["name"] == "Smith — wk3"
    assert tm.session_friendly_name.get(sid) == "Smith — wk3"


def test_non_therapist_cannot_set_friendly_name(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    tm.session_friendly_name.pop(sid, None)
    c.emit("set_friendly_name", {"session_id": sid, "user_id": "client-1", "name": "hax"})
    assert tm.session_friendly_name.get(sid) is None


def test_join_syncs_existing_friendly_name(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    tm.session_friendly_name[sid] = "Existing"
    sio = socketio.test_client(app, flask_test_client=enc_client)
    sio.emit("join", {"session_id": sid, "user_id": "c2", "mode": "group"})
    fn = [e for e in sio.get_received() if e["name"] == "friendly_name_set"]
    assert fn and fn[0]["args"][0]["name"] == "Existing" and fn[0]["args"][0].get("silent") is True
    tm.session_friendly_name.pop(sid, None)


# ---- Co-pilot chattiness (Batch B) ----

def test_copilot_should_emit_respects_cadence(enc_client):
    sid = "s-cad"
    tm.session_copilot_cadence[sid] = "stop"
    assert tm._copilot_should_emit(sid) is False
    tm.session_copilot_cadence[sid] = "more"
    assert tm._copilot_should_emit(sid) is True
    tm.session_copilot_cadence[sid] = "less"
    tm.session_copilot_emit_counter[sid] = 0
    assert [tm._copilot_should_emit(sid) for _ in range(3)] == [False, False, True]
    tm.session_copilot_cadence.pop(sid, None)
    tm.session_copilot_emit_counter.pop(sid, None)


def test_copilot_cadence_socket_therapist_only(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    tm.session_copilot_cadence.pop(sid, None)
    c.emit("copilot_cadence", {"session_id": sid, "user_id": "client-1", "mode": "stop"})
    assert tm.session_copilot_cadence.get(sid) is None        # client ignored
    t.emit("copilot_cadence", {"session_id": sid, "user_id": "ther-1", "mode": "stop"})
    assert tm.session_copilot_cadence.get(sid) == "stop"      # therapist applies
    tm.session_copilot_cadence.pop(sid, None)


def test_session_copilot_cards_returns_persisted(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
        tm._persist_cards(sid, [{"type": "risk", "text": "Risk note", "code": ""},
                                {"type": "reference", "text": "Ref", "code": "F32"}], "ther-1")
        cards = tm._session_copilot_cards(sid)
    assert len(cards) == 2
    assert cards[0]["type"] == "risk" and cards[1]["code"] == "F32"


def test_run_copilot_saves_even_when_stopped(enc_client):
    from models import CopilotCard
    with app.app_context():
        sid = _seed("ther-1")
        tm.session_copilot_cadence[sid] = "stop"
        with patch("copilot.build_risk_cards", return_value=[{"type": "risk", "text": "R", "confidence": 0.9}]), \
             patch("copilot.build_reference_cards", return_value=[]), \
             patch("copilot.generate_suggestions", return_value=[]), \
             patch("copilot.dedupe_cards", side_effect=lambda c, r: c), \
             patch.object(tm.socketio, "emit") as emit_mock:
            tm._run_copilot(sid, "group", trigger_text="hello", trigger_user_id="client-1")
        n = CopilotCard.query.filter_by(session_id=sid).count()
    assert n == 1                                            # saved to record despite "stop"
    emitted = [c for c in emit_mock.call_args_list if c.args and c.args[0] == "suggestion_cards"]
    assert not emitted                                      # but NOT shown live
    tm.session_copilot_cadence.pop(sid, None)
