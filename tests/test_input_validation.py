"""
tests/test_input_validation.py
------------------------------
Message validation now lives in the realtime SocketIO handler (on_send_message),
since the form-based consumer solo flow was removed. These tests exercise the
length / empty-message guards over a socket.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import uuid
import pytest
from datetime import datetime, timezone

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-input")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from TogetherMindsAI import app, socketio, _MAX_MSG_LEN
from models import db, User, TherapySession, init_encryption
from session_id import generate_session_id
from tests.socket_utils import authed_socket

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def session():
    """In-memory DB + a (consumer) couple session + a connected socket client.
    Returns (sio_client, session_id, user_id)."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        user_id = str(uuid.uuid4())
        sid = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="couple"))
        db.session.add(TherapySession(
            id=sid, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
        with app.test_client():
            # Identity is bound to the authenticated session; this session has no
            # therapist, so consent alone admits the user.
            sio = authed_socket(app, socketio, user_id, session_id=sid)
            sio.emit("join", {"session_id": sid, "mode": "couple"})
            sio.get_received()   # drain join noise
            yield sio, sid, user_id
        db.session.remove()
        db.drop_all()


def _names(received):
    return [e["name"] for e in received]


def test_oversized_message_emits_error(session):
    sio, sid, uid = session
    sio.emit("send_message", {
        "session_id": sid, "user_id": uid,
        "text": "a" * (_MAX_MSG_LEN + 1), "mode": "couple",
    })
    received = sio.get_received()
    assert "error" in _names(received)
    err = next(e for e in received if e["name"] == "error")
    assert "too long" in err["args"][0]["message"].lower()


def test_empty_message_is_ignored(session):
    sio, sid, uid = session
    sio.emit("send_message", {"session_id": sid, "user_id": uid, "text": "", "mode": "couple"})
    assert "new_message" not in _names(sio.get_received())


def test_whitespace_only_message_is_ignored(session):
    sio, sid, uid = session
    sio.emit("send_message", {"session_id": sid, "user_id": uid, "text": "   ", "mode": "couple"})
    assert "new_message" not in _names(sio.get_received())
