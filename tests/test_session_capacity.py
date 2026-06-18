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
