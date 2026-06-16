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
from TogetherMindsAI import app
from models import db, Clinician, TherapySession, init_encryption

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
