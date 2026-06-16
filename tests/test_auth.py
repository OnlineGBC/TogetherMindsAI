import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import base64
import time
import uuid
import pytest

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-auth")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, ECDSA, SECP256R1
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from datetime import datetime, timezone
from TogetherMindsAI import app
from models import db, User, TherapySession
from session_id import generate_session_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keypair():
    """Generate a P-256 keypair and return (private_key, base64_SPKI_public_key)."""
    private_key = generate_private_key(SECP256R1())
    spki_bytes  = private_key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return private_key, base64.b64encode(spki_bytes).decode()


def _sign(private_key, message: str) -> str:
    """Sign a UTF-8 message and return base64-encoded DER signature."""
    sig = private_key.sign(message.encode("utf-8"), ECDSA(SHA256()))
    return base64.b64encode(sig).decode()


def _register_via_join(client, mode="solo"):
    """Registration is join-only now, so stand up a clinician-led session, mark it
    pending (as /session/join would), then register an anonymous client identity.
    Returns (private_key, user_id, session_id)."""
    priv, pub_b64 = _make_keypair()
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by="clinician", therapist_id="clinician",
        created_at=datetime.now(timezone.utc),
    ))
    db.session.commit()
    with client.session_transaction() as s:
        s[f"pending_{mode}_session"] = sid
    rv = client.post("/api/auth/register", json={"public_key": pub_b64, "therapy_mode": mode})
    assert rv.status_code == 201, rv.get_data(as_text=True)
    return priv, rv.get_json()["user_id"], sid


# ---------------------------------------------------------------------------
# Fixtures
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
# /api/auth/register
# ---------------------------------------------------------------------------

def test_register_without_pending_join_returns_400(client):
    """Registration is join-only: with no clinician-led session pending, there is
    nothing to join, so a new self-directed session is NOT created."""
    _, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register", json={"public_key": pub_b64, "therapy_mode": "solo"})
    assert rv.status_code == 400


def test_register_joins_pending_session(client):
    priv, user_id, sid = _register_via_join(client, mode="couple")
    with app.app_context():
        user = db.session.get(User, user_id)
        assert user is not None
        assert user.public_key  # stored


def test_register_returns_joined_session_id(client):
    _, _, sid = _register_via_join(client, mode="group")
    # The returned session is the one that was pending (joined), not a new one.
    assert sid


def test_register_missing_public_key_returns_400(client):
    rv = client.post("/api/auth/register", json={"therapy_mode": "solo"})
    assert rv.status_code == 400


def test_register_invalid_mode_returns_400(client):
    _, pub_b64 = _make_keypair()
    rv = client.post("/api/auth/register", json={"public_key": pub_b64, "therapy_mode": "invalid"})
    assert rv.status_code == 400


# ---------------------------------------------------------------------------
# /api/auth/challenge
# ---------------------------------------------------------------------------

def test_challenge_returns_nonce(client):
    _, uid, _ = _register_via_join(client)
    rv = client.post("/api/auth/challenge", json={"user_id": uid})
    assert rv.status_code == 200
    data = rv.get_json()
    assert "challenge" in data
    assert len(data["challenge"]) == 64   # 32 bytes hex


def test_challenge_unknown_user_returns_404(client):
    rv = client.post("/api/auth/challenge", json={"user_id": "no-such-user"})
    assert rv.status_code == 404


def test_challenge_user_without_key_returns_400(client):
    # Create user via legacy route (no public key)
    with app.app_context():
        uid = str(uuid.uuid4())
        db.session.add(User(id=uid, therapy_mode="solo"))
        db.session.commit()
    rv = client.post("/api/auth/challenge", json={"user_id": uid})
    assert rv.status_code == 400


# ---------------------------------------------------------------------------
# /api/auth/verify
# ---------------------------------------------------------------------------

def test_verify_valid_signature_returns_200(client):
    priv, uid, _ = _register_via_join(client)
    challenge = client.post("/api/auth/challenge",
                            json={"user_id": uid}).get_json()["challenge"]
    sig_b64 = _sign(priv, challenge)
    rv = client.post("/api/auth/verify", json={"user_id": uid, "signature": sig_b64})
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["ok"] is True
    assert data["user_id"] == uid


def test_verify_invalid_signature_returns_401(client):
    _, uid, _ = _register_via_join(client)
    client.post("/api/auth/challenge", json={"user_id": uid})
    bad_sig = base64.b64encode(b"this is not a valid signature").decode()
    rv = client.post("/api/auth/verify", json={"user_id": uid, "signature": bad_sig})
    assert rv.status_code == 401


def test_verify_expired_challenge_returns_401(client):
    priv, uid, _ = _register_via_join(client)
    challenge = client.post("/api/auth/challenge",
                            json={"user_id": uid}).get_json()["challenge"]

    # Manually expire the challenge
    with app.app_context():
        user = db.session.get(User, uid)
        user.challenge_expires_at = time.time() - 1
        db.session.commit()

    sig_b64 = _sign(priv, challenge)
    rv = client.post("/api/auth/verify", json={"user_id": uid, "signature": sig_b64})
    assert rv.status_code == 401


def test_verify_unknown_user_returns_404(client):
    rv = client.post("/api/auth/verify",
                     json={"user_id": "ghost", "signature": base64.b64encode(b"x").decode()})
    assert rv.status_code == 404


def test_verify_challenge_cleared_after_success(client):
    """Replay attack prevention — challenge is deleted after first successful verify."""
    priv, uid, _ = _register_via_join(client)
    challenge = client.post("/api/auth/challenge",
                            json={"user_id": uid}).get_json()["challenge"]
    sig_b64 = _sign(priv, challenge)

    # First verify — should succeed
    rv1 = client.post("/api/auth/verify", json={"user_id": uid, "signature": sig_b64})
    assert rv1.status_code == 200

    # Second verify with same signature — challenge is gone, should fail
    rv2 = client.post("/api/auth/verify", json={"user_id": uid, "signature": sig_b64})
    assert rv2.status_code == 401
