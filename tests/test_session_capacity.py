"""Per-mode client capacity for the shared session engine.

Solo allows 1 client, couple allows 2, group is unlimited. "Clients" are
participants other than the session's therapist; the therapist is never counted
and a reconnecting/reloading client is always allowed back in. These assert the
`_session_is_full` gate used by both the room route and the SocketIO join.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-capacity")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

import pytest

from TogetherMindsAI import _session_is_full, room_participants

THERAPIST = "therapist-1"
SID = "sess-1"


@pytest.fixture(autouse=True)
def clear_participants():
    room_participants.clear()
    yield
    room_participants.clear()


def test_group_is_never_full():
    room_participants[SID] = {THERAPIST, "c1", "c2", "c3", "c4"}
    assert _session_is_full(SID, "group", "c5", THERAPIST) is False


def test_solo_allows_first_client_blocks_second():
    # Empty room → first client allowed.
    assert _session_is_full(SID, "solo", "c1", THERAPIST) is False
    # One client present → a different client is blocked.
    room_participants[SID] = {THERAPIST, "c1"}
    assert _session_is_full(SID, "solo", "c2", THERAPIST) is True


def test_therapist_is_never_counted_or_blocked():
    room_participants[SID] = {"c1"}  # a client is already present
    assert _session_is_full(SID, "solo", THERAPIST, THERAPIST) is False


def test_solo_reconnect_is_allowed():
    room_participants[SID] = {THERAPIST, "c1"}
    # The same client rejoining is excluded from the count → allowed.
    assert _session_is_full(SID, "solo", "c1", THERAPIST) is False


def test_couple_allows_two_blocks_third():
    room_participants[SID] = {THERAPIST, "c1"}
    assert _session_is_full(SID, "couple", "c2", THERAPIST) is False   # 2nd allowed
    room_participants[SID] = {THERAPIST, "c1", "c2"}
    assert _session_is_full(SID, "couple", "c3", THERAPIST) is True    # 3rd blocked


def test_couple_reconnect_is_allowed():
    room_participants[SID] = {THERAPIST, "c1", "c2"}
    assert _session_is_full(SID, "couple", "c2", THERAPIST) is False


# ---------------------------------------------------------------------------
# Capacity is enforced at the SocketIO JOIN (live presence), NOT at the HTTP render
# (whose presence is stale on polling transport and wrongly bounced returning clients
# to /welcome). So: the HTTP render must no longer bounce on a "full" room, and the
# socket join emits `session_full` for a genuine over-capacity client. Would have
# caught: a returning 1:1 client told the room is full and ejected to /welcome.
# ---------------------------------------------------------------------------
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())

from TogetherMindsAI import app, socketio
from models import db as _db, init_encryption, TherapySession, SessionStateCert
from session_id import generate_session_id
from tests.socket_utils import authed_socket, certify_state

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def cap_client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        _db.create_all()
        with app.test_client() as c:
            yield c
        _db.session.remove()
        _db.drop_all()


def _seed_solo(therapist="ther-cap"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    _db.session.add(TherapySession(
        id=sid, mode="solo", created_by=therapist, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    _db.session.commit()
    return sid, therapist


def test_http_render_does_not_bounce_on_full(cap_client):
    """A 'full' room must NOT eject a client at the HTTP render (presence is stale
    there). An un-consented client proceeds to the consent gate, never to /welcome."""
    with app.app_context():
        sid, ther = _seed_solo()
    room_participants[sid] = {ther, "client-A"}          # room looks full
    with cap_client.session_transaction() as s:
        s["user_id"] = "client-B"
    rv = cap_client.get(f"/therapy/solo/{sid}")
    loc = rv.headers.get("Location") or ""
    assert "session_full" not in loc                     # not bounced to the modal
    assert "/consent" in loc                             # proceeds to the consent gate


def test_socket_join_to_full_solo_emits_session_full(cap_client):
    """The SocketIO join is the capacity authority: a second client joining a solo
    (cap 1) is rejected with a `session_full` event, not admitted."""
    with app.app_context():
        sid, ther = _seed_solo()
        certify_state(_db, SessionStateCert, sid, ther, state="CA")
        t = authed_socket(app, socketio, ther, clinician=True)
        t.emit("join", {"session_id": sid, "mode": "solo"}); t.get_received()
        c1 = authed_socket(app, socketio, "cli-1", session_id=sid, state="CA")
        c1.emit("join", {"session_id": sid, "mode": "solo"}); c1.get_received()
        c2 = authed_socket(app, socketio, "cli-2", session_id=sid, state="CA")
        c2.emit("join", {"session_id": sid, "mode": "solo"})
        names = [e["name"] for e in c2.get_received()]
    assert "session_full" in names
