"""
tests/test_auth_oauth.py
------------------------
Clinician OAuth login (Google / Microsoft) and the logged-in therapist-start flow:
  - Clinician account is unique per (provider, subject)
  - /therapist and /therapist/start/<mode> require login
  - starting a session creates a clinician-owned session with clinical-record retention
  - the owning clinician can access the session transcript
  - the OAuth callback (mocked) creates the account and logs in
"""
import os
import sys
import uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from urllib.parse import unquote
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-oauth")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import TogetherMindsAI as tm
from TogetherMindsAI import app, socketio
from models import db, Clinician, ClientAccount, ChatMessage, TherapySession, SessionParticipant, init_encryption

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def client():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: eng}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def test_clinician_unique_provider_subject(client):
    db.session.add(Clinician(id="a", provider="google", provider_subject="sub1",
                             created_at=datetime.now(timezone.utc)))
    db.session.commit()
    db.session.add(Clinician(id="b", provider="google", provider_subject="sub1",
                             created_at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_therapist_page_requires_login(client):
    rv = client.get("/therapist")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]


def test_start_requires_login(client):
    rv = client.post("/therapist/start/solo")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]


def test_start_creates_clinician_owned_session_with_clinical_retention(client):
    cid = "clin-1"
    with client.session_transaction() as s:
        s["clinician_id"] = cid
        s["user_id"] = cid

    rv = client.post("/therapist/start/solo")
    assert rv.status_code == 302
    loc = rv.headers["Location"]
    assert "/therapy/solo/" in loc

    sid = unquote(loc.rsplit("/", 1)[-1])
    ts = db.session.get(TherapySession, sid)
    assert ts is not None
    assert ts.therapist_id == cid
    assert ts.created_by == cid
    # Clinical-record retention (~6 years), not the 30-day consumer purge.
    assert (ts.retention_expires_at - ts.created_at).days > 2000


def test_owning_clinician_can_access_transcript(client):
    cid = "clin-2"
    sid = tm.generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by=cid,
        created_at=datetime.now(timezone.utc), therapist_id=cid,
    ))
    db.session.commit()
    assert tm._user_can_access_session(sid, cid) is True
    assert tm._user_can_access_session(sid, "a-different-user") is False


def test_oauth_callback_creates_clinician_and_logs_in(client):
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "subj-xyz"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        rv = client.get("/auth/google/callback")

    assert rv.status_code == 302
    assert "/therapist" in rv.headers["Location"]

    clin = Clinician.query.filter_by(provider="google", provider_subject="subj-xyz").first()
    assert clin is not None
    with client.session_transaction() as s:
        assert s.get("clinician_id") == clin.id
        assert s.get("user_id") == clin.id


def test_oauth_callback_existing_clinician_reused(client):
    existing = Clinician(id="existing-id", provider="microsoft", provider_subject="ms-1",
                         created_at=datetime.now(timezone.utc))
    db.session.add(existing)
    db.session.commit()

    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "ms-1"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/auth/microsoft/callback")

    # No duplicate account created; the same id is reused.
    assert Clinician.query.filter_by(provider="microsoft", provider_subject="ms-1").count() == 1
    with client.session_transaction() as s:
        assert s.get("clinician_id") == "existing-id"


def test_oauth_callback_captures_clinician_email(client):
    """Phase 4 Step 3: the clinician's email claim is captured (encrypted) so we
    can send them their own recording links + retention notices."""
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {
        "userinfo": {"sub": "subj-email", "email": "Dr.Smith@Example.com"}
    }
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/auth/google/callback")

    clin = Clinician.query.filter_by(provider="google", provider_subject="subj-email").first()
    assert clin is not None
    assert clin.email == "dr.smith@example.com"   # normalised to lower-case


def test_oauth_callback_backfills_email_for_existing_clinician(client):
    existing = Clinician(id="existing-2", provider="google", provider_subject="g-2",
                         created_at=datetime.now(timezone.utc))
    db.session.add(existing)
    db.session.commit()
    assert existing.email is None

    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {
        "userinfo": {"sub": "g-2", "email": "back@fill.com"}
    }
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/auth/google/callback")

    refreshed = db.session.get(Clinician, "existing-2")
    assert refreshed.email == "back@fill.com"


# ---------------------------------------------------------------------------
# Microsoft multi-tenant issuer validation
# ---------------------------------------------------------------------------

def test_ms_issuer_accepts_tenant_substituted_issuer():
    """Regression: Azure 'common' returns a real per-tenant issuer; the default
    Authlib check (iss == templated metadata issuer) rejected every Microsoft
    sign-in. The validator must accept the issuer that matches the token's tid."""
    tid = "9188040d-6c67-4c5b-b112-36a304b66dad"
    claims = {"tid": tid}
    good = f"https://login.microsoftonline.com/{tid}/v2.0"
    assert tm._validate_ms_issuer(claims, good) is True


def test_ms_issuer_rejects_mismatched_tenant():
    claims = {"tid": "11111111-1111-1111-1111-111111111111"}
    wrong = "https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222/v2.0"
    assert tm._validate_ms_issuer(claims, wrong) is False


def test_ms_issuer_without_tid_accepts_well_formed_tenant_issuer():
    good = "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47/v2.0"
    assert tm._validate_ms_issuer({}, good) is True
    assert tm._validate_ms_issuer({}, "https://evil.example.com/x/v2.0") is False
    assert tm._validate_ms_issuer({}, "") is False


def test_callback_passes_iss_validator_for_microsoft_only(client):
    """The Microsoft callback must pass the custom iss validator to
    authorize_access_token; the Google callback must not."""
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "subj-ms"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/auth/microsoft/callback")
    ms_kwargs = fake_client.authorize_access_token.call_args.kwargs
    assert "claims_options" in ms_kwargs
    assert callable(ms_kwargs["claims_options"]["iss"]["validate"])

    fake_client.reset_mock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "subj-g"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/auth/google/callback")
    assert "claims_options" not in fake_client.authorize_access_token.call_args.kwargs


# ---------------------------------------------------------------------------
# Optional client login — separate account type, find your own past sessions
# ---------------------------------------------------------------------------

def test_client_account_unique_provider_subject(client):
    db.session.add(ClientAccount(id="a", provider="google", provider_subject="csub1",
                                 created_at=datetime.now(timezone.utc)))
    db.session.commit()
    db.session.add(ClientAccount(id="b", provider="google", provider_subject="csub1",
                                 created_at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_client_callback_creates_client_account_not_clinician(client):
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "csub-xyz"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        rv = client.get("/client/auth/google/callback")

    assert rv.status_code == 302
    assert "/me/sessions" in rv.headers["Location"]

    acct = ClientAccount.query.filter_by(provider="google", provider_subject="csub-xyz").first()
    assert acct is not None
    # A client sign-in must NOT mint a clinician account.
    assert Clinician.query.filter_by(provider="google", provider_subject="csub-xyz").first() is None
    with client.session_transaction() as s:
        assert s.get("client_account_id") == acct.id
        assert s.get("user_id") == acct.id
        assert s.get("clinician_id") is None


def test_client_callback_existing_account_reused(client):
    existing = ClientAccount(id="existing-client", provider="microsoft", provider_subject="cms-1",
                             created_at=datetime.now(timezone.utc))
    db.session.add(existing)
    db.session.commit()

    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "cms-1"}}
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        client.get("/client/auth/microsoft/callback")

    assert ClientAccount.query.filter_by(provider="microsoft", provider_subject="cms-1").count() == 1
    with client.session_transaction() as s:
        assert s.get("client_account_id") == "existing-client"


def test_my_sessions_requires_login(client):
    rv = client.get("/me/sessions")
    assert rv.status_code == 302
    assert "/client/login" in rv.headers["Location"]


def test_my_sessions_lists_only_own_participated_sessions(client):
    account_id = "client-77"
    # A session the client took part in (has a message), and one they did not.
    mine = TherapySession(id="SESS-MINE", mode="couple", created_by="therapist-1",
                          created_at=datetime.now(timezone.utc), therapist_id="therapist-1")
    other = TherapySession(id="SESS-OTHER", mode="couple", created_by="therapist-1",
                           created_at=datetime.now(timezone.utc), therapist_id="therapist-1")
    db.session.add_all([mine, other])
    db.session.add(ChatMessage(session_id="SESS-MINE", user_id=account_id,
                               display_name="Partner1", text="hello"))
    db.session.add(ChatMessage(session_id="SESS-OTHER", user_id="someone-else",
                               display_name="Partner2", text="not mine"))
    db.session.commit()

    with client.session_transaction() as s:
        s["client_account_id"] = account_id
        s["user_id"] = account_id

    rv = client.get("/me/sessions")
    assert rv.status_code == 200
    body = rv.data.decode()
    assert "SESS-MINE" in body
    assert "SESS-OTHER" not in body


def test_client_login_safe_next_rejects_open_redirect(client):
    # An absolute off-site "next" must not be stored / honoured.
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "csub-redir"}}
    client.get("/client/login?next=https://evil.example.com/phish")
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        rv = client.get("/client/auth/google/callback")
    # Falls back to the safe default, never the off-site URL.
    assert rv.headers["Location"].endswith("/me/sessions")


def test_client_login_safe_next_honours_relative_path(client):
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "csub-rel"}}
    client.get("/client/login?next=/therapy/couple/ABC123")
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        rv = client.get("/client/auth/google/callback")
    assert rv.headers["Location"].endswith("/therapy/couple/ABC123")


def test_clinician_oauth_login_stashes_return_target(client):
    # A download link opened on a phone sends them to /login?next=... — that target
    # is stashed at the OAuth-login step (survives the provider round-trip).
    with patch.object(tm, "_oauth_start", return_value="redirect-to-provider"):
        client.get("/auth/google/login?next=/session/transcript/stok/pdf")
    with client.session_transaction() as s:
        assert s.get("post_login_next") == "/session/transcript/stok/pdf"


def test_clinician_oauth_callback_returns_to_stashed_target(client):
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = {"userinfo": {"sub": "sub-next"}}
    with client.session_transaction() as s:
        s["post_login_next"] = "/session/transcript/stok/pdf"
    with patch.object(tm.oauth, "create_client", return_value=fake_client):
        rv = client.get("/auth/google/callback")
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith("/session/transcript/stok/pdf")


def test_clinician_oauth_login_ignores_offsite_next(client):
    with patch.object(tm, "_oauth_start", return_value="redirect-to-provider"):
        client.get("/auth/google/login?next=https://evil.example.com")
    with client.session_transaction() as s:
        assert s.get("post_login_next") is None   # open-redirect rejected


# ---------------------------------------------------------------------------
# Join-time participation tracking (silent attendees still get their session)
# ---------------------------------------------------------------------------

def test_session_participant_unique(client):
    now = datetime.now(timezone.utc)
    db.session.add(SessionParticipant(session_id="S1", user_id="u1", joined_at=now))
    db.session.commit()
    db.session.add(SessionParticipant(session_id="S1", user_id="u1", joined_at=now))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def _make_therapist_led_session(sid="SESS-JOIN", mode="couple"):
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by="therapist-1",
        created_at=datetime.now(timezone.utc), therapist_id="therapist-1",
    ))
    db.session.commit()
    return sid


def test_join_records_participation_idempotently(client):
    sid = _make_therapist_led_session()
    with client.session_transaction() as s:
        s["client_account_id"] = "cli-join"; s["user_id"] = "cli-join"
    sio = socketio.test_client(app, flask_test_client=client)
    # Join twice (reconnects happen constantly) — must not duplicate or error.
    sio.emit("join", {"session_id": sid, "user_id": "cli-join", "mode": "couple"})
    sio.emit("join", {"session_id": sid, "user_id": "cli-join", "mode": "couple"})
    sio.disconnect()

    rows = SessionParticipant.query.filter_by(session_id=sid, user_id="cli-join").all()
    assert len(rows) == 1


def test_my_sessions_lists_silent_attendee_session(client):
    """A client who joined but never sent a message must still see the session."""
    sid = _make_therapist_led_session(sid="SESS-SILENT")
    with client.session_transaction() as s:
        s["client_account_id"] = "cli-silent"; s["user_id"] = "cli-silent"
    sio = socketio.test_client(app, flask_test_client=client)
    sio.emit("join", {"session_id": sid, "user_id": "cli-silent", "mode": "couple"})
    sio.disconnect()

    # No ChatMessage exists for this client — only a participation row.
    assert ChatMessage.query.filter_by(session_id=sid, user_id="cli-silent").count() == 0
    rv = client.get("/me/sessions")
    assert rv.status_code == 200
    assert b"SESS-SILENT" in rv.data
