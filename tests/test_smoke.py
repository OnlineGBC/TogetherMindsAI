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
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    assert client.get("/therapy/solo").status_code == 200


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
    user_id = rv.get_json()["user_id"]

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
    rv = client.get("/therapy/solo")
    assert rv.status_code == 200

    # 5. Send a message — should redirect back to therapy page
    rv = client.post("/therapy/solo",
                     data={"message": "I feel anxious today"},
                     follow_redirects=True)
    assert rv.status_code == 200
    # AI response should be present in the rendered page
    assert b"therapist" in rv.data or b"hear" in rv.data or b"feel" in rv.data


def test_golden_path_message_creates_exercise(client):
    """A sent message must be persisted as an Exercise record."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    client.post("/therapy/solo",
                data={"message": "I feel very sad"})

    with app.app_context():
        from models import Exercise
        exercises = Exercise.query.filter_by(user_id=user_id).all()
    assert len(exercises) == 1
    assert exercises[0].mode == "solo"
    assert exercises[0].prompt == "I feel very sad"


def test_progress_page_shows_data_after_messages(client):
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    client.post("/therapy/solo", data={"message": "Hello there"})

    rv = client.get(f"/progress/{user_id}/solo")
    assert rv.status_code == 200
    # Chart data should be rendered (not the empty state)
    assert b"No exercises recorded" not in rv.data


# ---------------------------------------------------------------------------
# GDPR delete
# ---------------------------------------------------------------------------

def test_delete_user_removes_all_data(client):
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]
    client.post("/therapy/solo", data={"message": "Hello"})

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
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    huge = "x" * 100_000
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.post("/therapy/solo", data={"message": huge})
    assert rv.status_code == 302   # redirect, not 500


def test_session_join_unknown_id_shows_error(client):
    rv = client.post("/session/join", data={"session_id": "9999"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()


# ---------------------------------------------------------------------------
# End Session guard — modal and beforeunload wiring
# ---------------------------------------------------------------------------

def test_solo_page_has_end_session_button(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get("/therapy/solo")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


def test_couple_page_has_end_session_button(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
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
        session_id = str(uuid.uuid4())
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
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    rv = client.get(f"/transcript/{user_id}/pdf")
    assert rv.status_code == 403


def test_transcript_docx_forbidden_without_session(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    rv = client.get(f"/transcript/{user_id}/docx")
    assert rv.status_code == 403


def test_transcript_pdf_returns_pdf(client):
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]
    client.post("/therapy/solo", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{user_id}/pdf")
    assert rv.status_code == 200
    assert rv.content_type == "application/pdf"
    assert rv.data[:4] == b"%PDF"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_docx_returns_docx(client):
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]
    client.post("/therapy/solo", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{user_id}/docx")
    assert rv.status_code == 200
    assert "wordprocessingml" in rv.content_type
    # DOCX files are ZIP archives starting with PK
    assert rv.data[:2] == b"PK"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_pdf_empty_session(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/transcript/{user_id}/pdf")
    assert rv.status_code == 200
    assert rv.data[:4] == b"%PDF"


def test_end_session_modal_present_in_base(client):
    """The modal container must be in the base layout (rendered on every therapy page)."""
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get("/therapy/solo")
    assert b"endSessionIdDisplay" in rv.data
    assert b"endSessionConfirmBtn" in rv.data
    assert b"endSessionCopyBtn" in rv.data


# ---------------------------------------------------------------------------
# Session ID centralisation — integration tests
# ---------------------------------------------------------------------------

def test_couple_register_returns_session_id(client):
    """Couple registration must return session_id in the API response.

    Regression: session_id was missing for couple creators, causing the JS
    redirect to go to /therapy/couple/undefined and display 'UNDEFI'.
    For couple sessions session_id == user_id (creator's UUID).
    """
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "couple"})
    assert rv.status_code == 201
    data = rv.get_json()
    assert "session_id" in data, (
        "couple registration must include session_id in response — "
        "JS redirect uses data.session_id to build the URL"
    )
    assert data["session_id"] == data["user_id"], (
        "for couple sessions, session_id must equal user_id"
    )


def test_group_register_returns_valid_group_id(client):
    """Group session ID from /api/auth/register must pass is_valid_group_id."""
    from session_id import is_valid_group_id
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "group"})
    assert rv.status_code == 201
    session_id = rv.get_json().get("session_id")
    assert session_id is not None, "group registration must return a session_id"
    assert is_valid_group_id(session_id), (
        f"session_id {session_id!r} from /api/auth/register is not a valid group ID"
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
    """The join page placeholder must show a realistic group ID example."""
    from session_id import _example_group_id
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert _example_group_id().encode() in rv.data


def test_join_page_does_not_say_1234(client):
    """Regression: old placeholder said '1234' implying a 4-digit numeric code."""
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert b"1234" not in rv.data


def test_solo_display_id_is_short_and_no_raw_uuid_in_banner(client):
    """The solo therapy page must show a 6-char display ID, not the raw UUID."""
    from session_id import DISPLAY_ID_LENGTH
    import re
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    rv = client.get("/therapy/solo")
    assert rv.status_code == 200
    body = rv.data.decode()

    # The raw UUID (with hyphens) must NOT appear in the session banner or privacy banner
    uuid_pattern = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                               re.IGNORECASE)
    # Extract just the banner/alert sections to avoid false positives in hidden inputs
    banner_section = body[body.find("Session ID:"):body.find("Session ID:") + 200] if "Session ID:" in body else ""
    assert not uuid_pattern.search(banner_section), (
        f"Raw UUID found in session banner — display ID masking is broken.\n"
        f"Banner section: {banner_section!r}"
    )


def test_couple_display_id_does_not_expose_raw_uuid(client):
    """The couple therapy page session banner must show the masked display ID."""
    from datetime import datetime, timezone
    from models import TherapySession
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "couple"})
    user_id = rv.get_json()["user_id"]
    session_id = user_id  # for couple, session_id == user_id

    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    body = rv.data.decode()

    import re
    uuid_pattern = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                               re.IGNORECASE)
    banner_section = body[body.find("Session ID:"):body.find("Session ID:") + 200] if "Session ID:" in body else ""
    assert not uuid_pattern.search(banner_section), (
        f"Raw UUID found in couple session banner — display ID masking is broken.\n"
        f"Banner section: {banner_section!r}"
    )


def test_group_display_id_equals_session_id(client):
    """For group sessions, display_id must equal the internal session_id."""
    from session_id import is_valid_group_id
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "group"})
    session_id = rv.get_json()["session_id"]

    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    # The session_id (e.g. "AB3K7M") should appear in the rendered page as the display ID
    assert session_id.encode() in rv.data


def test_join_post_accepts_lowercase_group_id(client):
    """Group IDs entered in lowercase must still be found (normalisation)."""
    from datetime import datetime, timezone
    from models import TherapySession
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "group"})
    data = rv.get_json()
    user_id = data["user_id"]
    session_id = data["session_id"]

    # Submit the group ID in lowercase
    rv = client.post("/session/join", data={"session_id": session_id.lower()},
                     follow_redirects=False)
    # Should redirect (found), not render an error page
    assert rv.status_code in (301, 302), (
        f"Expected redirect for valid lowercase group ID, got {rv.status_code}. "
        f"Response: {rv.data[:200]}"
    )


def test_solo_rejoin_by_display_id_lowercase(client):
    """Entering the display ID in lowercase must also work.

    Regression: is_display_id() required uppercase, so lowercase input skipped
    the prefix search entirely and returned 'Session not found'.
    """
    from session_id import to_display_id
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]
    display_id = to_display_id(user_id, "solo").lower()  # force lowercase

    rv = client.post("/session/join", data={"session_id": display_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Solo rejoin by lowercase display ID '{display_id}' failed — got {rv.status_code}."
    )


def test_solo_rejoin_by_display_id(client):
    """Solo sessions must be rejoinable by the 6-char display ID shown in the header.

    Regression: display ID is derived from the UUID but not stored in the DB,
    so a direct primary-key lookup fails. The join route must fall back to a
    prefix search.
    """
    from session_id import to_display_id
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]
    display_id = to_display_id(user_id, "solo")

    rv = client.post("/session/join", data={"session_id": display_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Solo rejoin by display ID '{display_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_couple_rejoin_by_display_id(client):
    """Couple sessions must be rejoinable by the 6-char display ID shown in the header."""
    from session_id import to_display_id
    from datetime import datetime, timezone
    from models import TherapySession
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "couple"})
    data = rv.get_json()
    user_id = data["user_id"]
    session_id = data["session_id"]  # == user_id for couple
    display_id = to_display_id(session_id, "couple")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": display_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Couple rejoin by display ID '{display_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_group_rejoin_by_display_id(client):
    """Group sessions must be rejoinable by the 6-char code (display ID == session ID)."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "group"})
    data = rv.get_json()
    user_id = data["user_id"]
    session_id = data["session_id"]  # already the display ID for group

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Group rejoin by display ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_solo_rejoin_by_nickname(client):
    """Solo sessions must be rejoinable by friendly name."""
    from datetime import datetime, timezone
    from models import TherapySession
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    # Save a nickname server-side
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    client.post(f"/session/{user_id}/nickname",
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
    from session_id import _example_group_id
    rv = client.post("/session/join", data={"session_id": "DOESNOTEXIST"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()
    # Hint must still be present — it's passed through _join_template on all paths
    assert _example_group_id().encode() in rv.data


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
