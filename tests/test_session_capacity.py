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
# Route-level: the client-enter gate exempts a RETURNING consented client (so a
# stale-presence race can't wrongly tell them the room is full) and sends a genuine
# un-consented newcomer to the acknowledgement modal (welcome?session_full=1).
# Would have caught: a returning 1:1 client being told "That session is already full".
# ---------------------------------------------------------------------------
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode())

from TogetherMindsAI import app
from models import db as _db, init_encryption, TherapySession
from session_id import generate_session_id

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


def test_newcomer_to_full_solo_redirects_to_modal(cap_client):
    with app.app_context():
        sid, ther = _seed_solo()
    room_participants[sid] = {ther, "client-A"}          # a client slot is taken
    with cap_client.session_transaction() as s:
        s["user_id"] = "client-B"                         # un-consented newcomer
    rv = cap_client.get(f"/therapy/solo/{sid}")
    assert rv.status_code in (302, 303)
    assert "session_full=1" in rv.headers.get("Location", "")


def test_returning_consented_client_is_exempt_from_full(cap_client):
    with app.app_context():
        sid, ther = _seed_solo()
    room_participants[sid] = {ther, "client-A"}          # slot occupied (e.g. stale ghost)
    with cap_client.session_transaction() as s:
        s["user_id"] = "client-B"                         # would be blocked WITHOUT the exemption
        s["consented_sessions"] = [sid]                   # …but already consented → returning client
    rv = cap_client.get(f"/therapy/solo/{sid}")
    # Must NOT be bounced to the capacity modal.
    assert "session_full=1" not in (rv.headers.get("Location") or "")
