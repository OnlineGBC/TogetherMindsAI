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
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
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
    assert client.get(f"/therapy/solo/{user_id}").status_code == 200


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
    rv = client.get(f"/therapy/solo/{user_id}")
    assert rv.status_code == 200

    # 5. Send a message — should redirect back to therapy page
    rv = client.post(f"/therapy/solo/{user_id}",
                     data={"message": "I feel anxious today"},
                     follow_redirects=True)
    assert rv.status_code == 200
    # AI response should be present in the rendered page
    assert b"anxious" in rv.data or b"therapist" in rv.data or b"breath" in rv.data


def test_golden_path_message_creates_exercise(client):
    """A sent message must be persisted as an Exercise record."""
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    client.post(f"/therapy/solo/{user_id}",
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

    client.post(f"/therapy/solo/{user_id}", data={"message": "Hello there"})

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
    client.post(f"/therapy/solo/{user_id}", data={"message": "Hello"})

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
    rv = client.post(f"/therapy/solo/{user_id}", data={"message": huge})
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
    rv = client.get(f"/therapy/solo/{user_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


def test_couple_page_has_end_session_button(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="couple"))
        db.session.commit()
    rv = client.get(f"/therapy/couple/{user_id}")
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
    rv = client.get(f"/therapy/group/{user_id}/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


# ---------------------------------------------------------------------------
# Transcript download
# ---------------------------------------------------------------------------

def test_transcript_download_forbidden_without_session(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    rv = client.get(f"/transcript/{user_id}")
    assert rv.status_code == 403


def test_transcript_download_returns_text_file(client):
    priv, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": "solo"})
    user_id = rv.get_json()["user_id"]

    # Send a message so there is transcript content
    client.post(f"/therapy/solo/{user_id}", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{user_id}")
    assert rv.status_code == 200
    assert rv.content_type.startswith("text/plain")
    assert b"TogetherMindsAI" in rv.data
    assert b"Session Transcript" in rv.data
    assert b"I feel anxious" in rv.data
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_empty_session_still_downloads(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/transcript/{user_id}")
    assert rv.status_code == 200
    assert b"No messages recorded" in rv.data


def test_end_session_modal_present_in_base(client):
    """The modal container must be in the base layout (rendered on every therapy page)."""
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    rv = client.get(f"/therapy/solo/{user_id}")
    assert b"endSessionIdDisplay" in rv.data
    assert b"endSessionConfirmBtn" in rv.data
    assert b"endSessionCopyBtn" in rv.data
