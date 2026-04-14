"""
Smoke tests — verify the app starts, all key pages return correct status codes,
and the full golden-path (register → challenge → verify → send message) works
end-to-end against a real in-process server.

These tests exercise the production code path including eventlet/SocketIO setup,
database schema, and HTTP routing — things unit tests with mocked internals cannot catch.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import base64
import sqlite3
import uuid
import pytest

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-smoke")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, ECDSA, SECP256R1
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from TogetherMindsAI import app
from models import db, User
from session_id import generate_session_id, is_valid_session_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keypair():
    priv = generate_private_key(SECP256R1())
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    return priv, pub_b64


def _sign(priv, message: str) -> str:
    return base64.b64encode(priv.sign(message.encode(), ECDSA(SHA256()))).decode()


def _register(client, mode="solo"):
    """Register a new user and return (priv_key, user_id, session_id)."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": mode})
    assert rv.status_code == 201
    data = rv.get_json()
    return priv, data["user_id"], data.get("session_id")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    # Flask-SQLAlchemy 3.x caches the engine — changing app.config["SQLALCHEMY_DATABASE_URI"]
    # after init_app() has no effect. Override the cached engine directly so that
    # db.create_all() / db.drop_all() operate on an isolated in-memory DB, not the
    # production file.
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# Page availability — every route returns the expected status code
# ---------------------------------------------------------------------------

def test_home_page_returns_200(client):
    assert client.get("/").status_code == 200


def test_tos_page_returns_200(client):
    rv = client.get("/tos")
    assert rv.status_code == 200
    assert b"Terms of Service" in rv.data
    assert b"togethermindsai@onlinegbc.com" in rv.data
    assert b"findahelpline.com" in rv.data


def test_auth_solo_page_returns_200(client):
    assert client.get("/auth/solo").status_code == 200


def test_auth_couple_page_returns_200(client):
    assert client.get("/auth/couple").status_code == 200


def test_auth_group_page_returns_200(client):
    assert client.get("/auth/group").status_code == 200


def test_session_join_page_returns_200(client):
    assert client.get("/session/join").status_code == 200


def test_progress_page_returns_200(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    assert client.get(f"/progress/{user_id}/solo").status_code == 200


def test_therapy_solo_page_returns_200(client):
    priv, user_id, session_id = _register(client, "solo")
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    assert client.get(f"/therapy/solo/{session_id}").status_code == 200


# ---------------------------------------------------------------------------
# DB schema — all expected columns are present
# ---------------------------------------------------------------------------

def test_users_table_has_auth_columns(client):
    with app.app_context():
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        col_names = {c["name"] for c in inspector.get_columns("users")}
    expected = {"id", "therapy_mode", "public_key", "challenge", "challenge_expires_at"}
    assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"


def test_rate_limit_entries_table_exists(client):
    with app.app_context():
        from sqlalchemy import inspect
        tables = inspect(db.engine).get_table_names()
    assert "rate_limit_entries" in tables


def test_therapy_sessions_table_exists(client):
    with app.app_context():
        from sqlalchemy import inspect
        tables = inspect(db.engine).get_table_names()
    assert "therapy_sessions" in tables


def test_exercises_table_has_mode_column(client):
    with app.app_context():
        from sqlalchemy import inspect
        col_names = {c["name"] for c in inspect(db.engine).get_columns("exercises")}
    assert "mode" in col_names


# ---------------------------------------------------------------------------
# Golden path — register → challenge → verify → load therapy → send message
# ---------------------------------------------------------------------------

def test_golden_path_solo(client):
    priv, pub_b64 = _make_keypair()

    # 1. Register
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    assert rv.status_code == 201
    data = rv.get_json()
    user_id = data["user_id"]
    session_id = data["session_id"]

    # 2. Challenge
    rv = client.post("/api/auth/challenge", json={"user_id": user_id})
    assert rv.status_code == 200
    challenge = rv.get_json()["challenge"]

    # 3. Verify
    rv = client.post("/api/auth/verify",
                     json={"user_id": user_id, "signature": _sign(priv, challenge)})
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True

    # 4. Load therapy page
    rv = client.get(f"/therapy/solo/{session_id}")
    assert rv.status_code == 200

    # 5. Send a message — should redirect back to therapy page
    rv = client.post(f"/therapy/solo/{session_id}",
                     data={"message": "I feel anxious today"},
                     follow_redirects=True)
    assert rv.status_code == 200
    # AI response should be present in the rendered page
    assert b"therapist" in rv.data or b"hear" in rv.data or b"feel" in rv.data


def test_golden_path_message_creates_exercise(client):
    """A sent message must be persisted as an Exercise record."""
    priv, user_id, session_id = _register(client, "solo")

    client.post(f"/therapy/solo/{session_id}",
                data={"message": "I feel very sad"})

    with app.app_context():
        from models import Exercise
        exercises = Exercise.query.filter_by(user_id=user_id).all()
    assert len(exercises) == 1
    assert exercises[0].mode == "solo"
    assert exercises[0].type == "solo_chat"


def test_progress_page_shows_data_after_messages(client):
    priv, user_id, session_id = _register(client, "solo")

    client.post(f"/therapy/solo/{session_id}", data={"message": "Hello there"})

    rv = client.get(f"/progress/{user_id}/solo")
    assert rv.status_code == 200
    # Chart data should be rendered (not the empty state)
    assert b"No exercises recorded" not in rv.data


# ---------------------------------------------------------------------------
# GDPR delete
# ---------------------------------------------------------------------------

def test_delete_user_removes_all_data(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "Hello"})

    rv = client.delete(f"/user/{user_id}")
    assert rv.status_code == 200

    with app.app_context():
        assert db.session.get(User, user_id) is None
        from models import Exercise, ChatMessage
        assert Exercise.query.filter_by(user_id=user_id).count() == 0
        assert ChatMessage.query.filter_by(user_id=user_id).count() == 0


def test_delete_user_forbidden_for_wrong_session(client):
    with app.app_context():
        other_id = str(uuid.uuid4())
        db.session.add(User(id=other_id, therapy_mode="solo"))
        db.session.commit()
    # Client has no session set — should get 403
    rv = client.delete(f"/user/{other_id}")
    assert rv.status_code == 403


# ---------------------------------------------------------------------------
# Input validation — boundary conditions
# ---------------------------------------------------------------------------

def test_oversized_message_does_not_crash_server(client):
    priv, user_id, session_id = _register(client, "solo")
    huge = "x" * 100_000
    rv = client.post(f"/therapy/solo/{session_id}", data={"message": huge})
    assert rv.status_code in (302, 422)   # redirect or error page, not 500
    if rv.status_code == 422:
        assert b"too long" in rv.data.lower()


def test_session_join_unknown_id_shows_error(client):
    rv = client.post("/session/join", data={"session_id": "9999"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()


# ---------------------------------------------------------------------------
# End Session guard — modal and beforeunload wiring
# ---------------------------------------------------------------------------

def test_solo_page_has_end_session_button(client):
    priv, user_id, session_id = _register(client, "solo")
    rv = client.get(f"/therapy/solo/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


def test_couple_page_has_end_session_button(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="couple"))
        db.session.add(TherapySession(
            id=session_id, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


def test_group_page_has_end_session_button(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="group"))
        db.session.add(TherapySession(
            id=session_id, mode="group", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


# ---------------------------------------------------------------------------
# Transcript download
# ---------------------------------------------------------------------------

def test_transcript_pdf_forbidden_without_session(client):
    """No flask session → 403 regardless of session_id."""
    fake_session_id = generate_session_id()
    rv = client.get(f"/transcript/{fake_session_id}/pdf")
    assert rv.status_code == 403


def test_transcript_docx_forbidden_without_session(client):
    """No flask session → 403 regardless of session_id."""
    fake_session_id = generate_session_id()
    rv = client.get(f"/transcript/{fake_session_id}/docx")
    assert rv.status_code == 403


def test_transcript_pdf_returns_pdf(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{session_id}/pdf")
    assert rv.status_code == 200
    assert rv.content_type == "application/pdf"
    assert rv.data[:4] == b"%PDF"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_docx_returns_docx(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{session_id}/docx")
    assert rv.status_code == 200
    assert "wordprocessingml" in rv.content_type
    # DOCX files are ZIP archives starting with PK
    assert rv.data[:2] == b"PK"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_pdf_empty_session(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.add(TherapySession(
            id=session_id, mode="solo", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/transcript/{session_id}/pdf")
    assert rv.status_code == 200
    assert rv.data[:4] == b"%PDF"


def test_end_session_modal_present_in_base(client):
    """The modal container must be in the base layout (rendered on every therapy page)."""
    priv, user_id, session_id = _register(client, "solo")
    rv = client.get(f"/therapy/solo/{session_id}")
    assert b"endSessionIdDisplay" in rv.data
    assert b"endSessionConfirmBtn" in rv.data
    assert b"endSessionCopyBtn" in rv.data


# ---------------------------------------------------------------------------
# Session ID centralisation — integration tests
# ---------------------------------------------------------------------------

def test_solo_register_returns_valid_session_id(client):
    """Solo registration must return a valid 6-char session_id, not a UUID."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    assert rv.status_code == 201
    data = rv.get_json()
    assert "session_id" in data, "solo registration must include session_id in response"
    assert is_valid_session_id(data["session_id"]), (
        f"solo session_id {data['session_id']!r} is not a valid 6-char session ID"
    )
    assert data["session_id"] != data["user_id"], (
        "session_id must be independent of user_id (no longer UUID-based)"
    )


def test_couple_register_returns_valid_session_id(client):
    """Couple registration must return a valid 6-char session_id."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "couple"})
    assert rv.status_code == 201
    data = rv.get_json()
    assert "session_id" in data, (
        "couple registration must include session_id in response — "
        "JS redirect uses data.session_id to build the URL"
    )
    assert is_valid_session_id(data["session_id"]), (
        f"couple session_id {data['session_id']!r} is not a valid 6-char session ID"
    )


def test_group_register_returns_valid_session_id(client):
    """Group session ID from /api/auth/register must pass is_valid_session_id."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "group"})
    assert rv.status_code == 201
    session_id = rv.get_json().get("session_id")
    assert session_id is not None, "group registration must return a session_id"
    assert is_valid_session_id(session_id), (
        f"session_id {session_id!r} from /api/auth/register is not a valid session ID"
    )


def test_all_modes_return_different_session_and_user_ids(client):
    """For every mode, session_id must be distinct from user_id (no longer UUID-derived)."""
    for mode in ("solo", "couple", "group"):
        priv, user_id, session_id = _register(client, mode)
        assert session_id != user_id, (
            f"{mode}: session_id should be a 6-char code, not the user UUID"
        )
        assert is_valid_session_id(session_id), (
            f"{mode}: session_id {session_id!r} is not a valid 6-char session ID"
        )


def test_join_page_contains_dynamic_hint(client):
    """The join page help text must come from the module, not hardcoded HTML."""
    rv = client.get("/session/join")
    assert rv.status_code == 200
    body = rv.data.decode()
    # Must contain the correct length (6) not the stale "4-digit"
    assert "6" in body
    assert "4-digit" not in body


def test_join_page_placeholder_uses_charset_example(client):
    """The join page placeholder must show a realistic session ID example."""
    from session_id import _example_session_id
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert _example_session_id().encode() in rv.data


def test_join_page_does_not_say_1234(client):
    """Regression: old placeholder said '1234' implying a 4-digit numeric code."""
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert b"1234" not in rv.data


def test_solo_page_shows_session_id_in_banner(client):
    """The solo therapy page must show the 6-char session ID, not a UUID."""
    priv, user_id, session_id = _register(client, "solo")

    rv = client.get(f"/therapy/solo/{session_id}")
    assert rv.status_code == 200
    body = rv.data.decode()

    # The 6-char session ID must appear in the page
    assert session_id in body, f"session_id {session_id!r} not found in solo page"

    # No raw UUID (with hyphens) should appear in the session banner area
    import re
    uuid_pattern = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE
    )
    banner_start = body.find("Session ID:")
    banner_section = body[banner_start:banner_start + 200] if banner_start != -1 else ""
    assert not uuid_pattern.search(banner_section), (
        f"Raw UUID found in session banner — 6-char IDs should not contain hyphens.\n"
        f"Banner section: {banner_section!r}"
    )


def test_couple_page_shows_session_id_in_banner(client):
    """The couple therapy page session banner must show the 6-char session ID."""
    priv, user_id, session_id = _register(client, "couple")

    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    body = rv.data.decode()

    assert session_id in body, f"session_id {session_id!r} not found in couple page"

    import re
    uuid_pattern = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE
    )
    banner_start = body.find("Session ID:")
    banner_section = body[banner_start:banner_start + 200] if banner_start != -1 else ""
    assert not uuid_pattern.search(banner_section), (
        f"Raw UUID found in couple session banner.\nBanner section: {banner_section!r}"
    )


def test_group_page_shows_session_id_in_banner(client):
    """For group sessions, the 6-char session_id should appear in the rendered page."""
    priv, user_id, session_id = _register(client, "group")

    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    assert session_id.encode() in rv.data


def test_join_post_session_id_is_case_sensitive(client):
    """Session IDs are case-sensitive: lowercase of a valid ID must not be found.

    Architecture: IDs are stored and compared exactly as generated.
    A session stored as 'aB3k7M' cannot be found by 'ab3k7m'.
    """
    priv, user_id, session_id = _register(client, "group")

    # Flip the case of each character to guarantee a mismatch
    flipped = session_id.swapcase()
    # Only test if the flipped version is actually different (it always will be for mixed-case)
    if flipped == session_id:
        pytest.skip("generated ID has no case distinction — skipping")

    rv = client.post("/session/join", data={"session_id": flipped},
                     follow_redirects=False)
    # Should NOT redirect (not found), should render error
    assert rv.status_code == 200, (
        f"Expected error page for wrong-case ID '{flipped}', got redirect. "
        f"Session IDs must be case-sensitive."
    )
    assert b"not found" in rv.data.lower()


def test_solo_rejoin_by_session_id(client):
    """Solo sessions must be rejoinable by the exact 6-char session ID."""
    priv, user_id, session_id = _register(client, "solo")

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Solo rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_couple_rejoin_by_session_id(client):
    """Couple sessions must be rejoinable by the exact 6-char session ID."""
    priv, user_id, session_id = _register(client, "couple")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Couple rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_group_rejoin_by_session_id(client):
    """Group sessions must be rejoinable by the exact 6-char session ID."""
    priv, user_id, session_id = _register(client, "group")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Group rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_solo_rejoin_by_nickname(client):
    """Solo sessions must be rejoinable by friendly name."""
    priv, user_id, session_id = _register(client, "solo")

    # Save a nickname server-side
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    client.post(f"/session/{session_id}/nickname",
                json={"nickname": "My Monday session"},
                content_type="application/json")

    rv = client.post("/session/join", data={"session_id": "My Monday session"},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Solo rejoin by nickname failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_join_error_page_still_shows_hint(client):
    """Even on error, the join page must render the format hint (not blank)."""
    from session_id import _example_session_id
    rv = client.post("/session/join", data={"session_id": "DOESNOTEXIST"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()
    # Hint must still be present — it's passed through _join_template on all paths
    assert _example_session_id().encode() in rv.data


# ---------------------------------------------------------------------------
# WebSocket upgrade disabled under werkzeug
# ---------------------------------------------------------------------------

def test_socketio_upgrades_disabled_in_dev_and_test_mode():
    """Regression: werkzeug crashes with AssertionError when socket.io clients
    attempt a WebSocket upgrade (transport=websocket POST returns 500).
    The SocketIO instance must disable upgrades whenever FLASK_DEBUG or
    IS_TESTING is True so clients never make the upgrade attempt."""
    import config
    from TogetherMindsAI import socketio
    is_werkzeug_env = config.FLASK_DEBUG or config.IS_TESTING
    if is_werkzeug_env:
        assert socketio.server.eio.allow_upgrades is False, (
            "allow_upgrades must be False under werkzeug (FLASK_DEBUG or IS_TESTING) "
            "to prevent the AssertionError: write() before start_response crash"
        )
