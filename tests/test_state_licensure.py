"""
tests/test_state_licensure.py
-----------------------------
Per-session licensure gate: a client is admitted only once the clinician has
certified they're authorised to see clients in the client's attested U.S. state.

The whole path rides plain HTTP (no sockets):
  - consent POST carries the client's state + location attestation,
  - the presence heartbeat POST surfaces pending states to the clinician,
  - a small certify POST records the decision and releases/turns away clients.

Covered here: hold vs admit vs turn-away, U.S.-only block, per-state dedup,
therapist-only certification, heartbeat reporting, direct-URL enforcement, and
the audit trail.
"""
import os
import sys
import uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-licensure")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import TogetherMindsAI as tm
from TogetherMindsAI import app, session_pending_state
from models import db, init_encryption, TherapySession, SessionStateCert, AuditLog
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
    session_pending_state.clear()          # module global — don't leak across tests
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


THER = "ther-1"


def _seed_session(mode="group"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by=THER, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=THER,
    ))
    db.session.commit()
    return sid


def _actor(user_id):
    """A test client whose session is signed in as `user_id` (own cookie jar)."""
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = user_id
    return c


def _consent(actor_client, sid, state):
    return actor_client.post(f"/session/{sid}/consent",
                             data={"state": state, "location_attest": "1"})


def _cert_row(sid, state):
    return SessionStateCert.query.filter_by(session_id=sid, state=state).first()


# ---------------------------------------------------------------------------
# Consent gate — hold / admit / turn away
# ---------------------------------------------------------------------------

def test_uncertified_state_is_held(enc_client):
    with app.app_context():
        sid = _seed_session()
    rv = _consent(_actor("client-1"), sid, "NJ")
    assert rv.status_code == 302
    assert "/state-gate" in rv.headers["Location"]
    with app.app_context():
        # attestation audited; client registered as pending for the heartbeat
        assert AuditLog.query.filter_by(event_type="client_location_attested", session_id=sid).count() == 1
    assert session_pending_state.get(sid, {}).get("client-1") == "NJ"


def test_certified_state_is_admitted(enc_client):
    with app.app_context():
        sid = _seed_session()
        db.session.add(SessionStateCert(session_id=sid, state="NJ", therapist_id=THER,
                                        decision="certified"))
        db.session.commit()
    rv = _consent(_actor("client-1"), sid, "NJ")
    assert rv.status_code == 302
    assert f"/therapy/group/{sid}" in rv.headers["Location"]


def test_declined_state_is_turned_away(enc_client):
    with app.app_context():
        sid = _seed_session()
        db.session.add(SessionStateCert(session_id=sid, state="NJ", therapist_id=THER,
                                        decision="declined"))
        db.session.commit()
    rv = _consent(_actor("client-1"), sid, "NJ")
    assert rv.status_code == 403
    assert b"isn't able to see clients" in rv.data


def test_outside_us_is_blocked(enc_client):
    with app.app_context():
        sid = _seed_session()
    rv = _consent(_actor("client-1"), sid, "INTL")
    assert rv.status_code == 403
    assert b"United States" in rv.data


def test_invalid_state_is_blocked(enc_client):
    with app.app_context():
        sid = _seed_session()
    rv = _consent(_actor("client-1"), sid, "ZZ")
    assert rv.status_code == 403


# ---------------------------------------------------------------------------
# Certification — records, dedups, releases; therapist-only
# ---------------------------------------------------------------------------

def test_certify_records_and_dedups(enc_client):
    with app.app_context():
        sid = _seed_session()
    # first NJ client is held
    c1 = _actor("client-1")
    assert "/state-gate" in _consent(c1, sid, "NJ").headers["Location"]

    ther = _actor(THER)
    rv = ther.post(f"/session/{sid}/certify-state", json={"state": "NJ", "decision": "certify"})
    assert rv.status_code == 200 and rv.get_json()["decision"] == "certified"

    with app.app_context():
        assert _cert_row(sid, "NJ").decision == "certified"
        assert AuditLog.query.filter_by(event_type="state_certified", session_id=sid).count() == 1

    # a SECOND NJ client is admitted straight away (dedup — no second prompt)
    c2 = _actor("client-2")
    rv2 = _consent(c2, sid, "NJ")
    assert f"/therapy/group/{sid}" in rv2.headers["Location"]
    # the first client can now enter the room too
    assert c1.get(f"/therapy/group/{sid}").status_code == 200


def test_decline_turns_away_waiting_client(enc_client):
    with app.app_context():
        sid = _seed_session()
    c1 = _actor("client-1")
    _consent(c1, sid, "NJ")                       # held
    _actor(THER).post(f"/session/{sid}/certify-state", json={"state": "NJ", "decision": "decline"})
    with app.app_context():
        assert _cert_row(sid, "NJ").decision == "declined"
        assert AuditLog.query.filter_by(event_type="state_declined", session_id=sid).count() == 1
    # the waiting client now sees the turn-away page
    rv = c1.get(f"/session/{sid}/state-gate")
    assert rv.status_code == 403
    assert b"isn't able to see clients" in rv.data


def test_certify_is_therapist_only(enc_client):
    with app.app_context():
        sid = _seed_session()
    rv = _actor("client-1").post(f"/session/{sid}/certify-state",
                                 json={"state": "NJ", "decision": "certify"})
    assert rv.status_code == 403
    with app.app_context():
        assert _cert_row(sid, "NJ") is None


# ---------------------------------------------------------------------------
# Heartbeat surfaces pending states (no sockets)
# ---------------------------------------------------------------------------

def test_heartbeat_reports_then_clears_pending(enc_client):
    with app.app_context():
        sid = _seed_session()
    _consent(_actor("client-1"), sid, "NJ")
    ther = _actor(THER)

    pend = ther.post(f"/session/{sid}/heartbeat").get_json()["pending_states"]
    assert pend == [{"code": "NJ", "name": "New Jersey", "count": 1}]

    ther.post(f"/session/{sid}/certify-state", json={"state": "NJ", "decision": "certify"})
    assert ther.post(f"/session/{sid}/heartbeat").get_json()["pending_states"] == []


# ---------------------------------------------------------------------------
# Direct-URL enforcement — a consented-but-uncertified client can't bypass
# ---------------------------------------------------------------------------

def test_room_url_cannot_bypass_gate(enc_client):
    with app.app_context():
        sid = _seed_session()
    c1 = _actor("client-1")
    _consent(c1, sid, "NJ")                       # consented, but NJ not yet certified
    rv = c1.get(f"/therapy/group/{sid}")          # try the room directly
    assert rv.status_code == 302
    assert "/state-gate" in rv.headers["Location"]
