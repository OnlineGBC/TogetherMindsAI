"""
tests/test_csrf.py
------------------
CSRF protection (HIPAA / finding 3). Enforcement is disabled by default under
the test runner (so the suite needn't thread a token through every POST — the
standard Flask-WTF testing behaviour); these tests flip it ON to verify it.

Covers: state-changing POST rejected without a token, accepted with a valid
token (header OR form field), rejected with a wrong token, and that
signature-authenticated endpoints (Stripe webhook, ECDSA client-auth) are exempt.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-csrf")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from TogetherMindsAI import app
from models import db, init_encryption

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


def _fresh_db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db._app_engines[app] = {None: engine}


@pytest.fixture
def csrf_client():
    """Test client with CSRF enforcement turned ON (off by default in tests)."""
    _fresh_db()
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = True
    try:
        with app.app_context():
            db.create_all()
            with app.test_client() as c:
                yield c
            db.session.remove()
            db.drop_all()
    finally:
        app.config.pop("CSRF_ENABLED", None)   # never leak enforcement to other test files


def _is_csrf_reject(rv):
    return rv.status_code == 400 and (rv.get_json() or {}).get("error") == "csrf_invalid"


# A state-changing route protected by CSRF. The before_request guard runs before
# the view's own auth, so login state is irrelevant to the CSRF outcome.
PROTECTED = "/therapist/start/solo"


def test_protected_post_without_token_is_rejected(csrf_client):
    assert _is_csrf_reject(csrf_client.post(PROTECTED))


def test_valid_header_token_passes_csrf(csrf_client):
    with csrf_client.session_transaction() as s:
        s["_csrf_token"] = "tok-123"
    rv = csrf_client.post(PROTECTED, headers={"X-CSRFToken": "tok-123"})
    assert not _is_csrf_reject(rv)          # CSRF cleared (route may still redirect/403 for auth)


def test_valid_form_token_passes_csrf(csrf_client):
    with csrf_client.session_transaction() as s:
        s["_csrf_token"] = "tok-abc"
    rv = csrf_client.post(PROTECTED, data={"csrf_token": "tok-abc"})
    assert not _is_csrf_reject(rv)


def test_wrong_token_is_rejected(csrf_client):
    with csrf_client.session_transaction() as s:
        s["_csrf_token"] = "right"
    assert _is_csrf_reject(csrf_client.post(PROTECTED, headers={"X-CSRFToken": "wrong"}))


def test_stripe_webhook_is_exempt(csrf_client):
    # Exempt (Stripe signature): no token, must NOT be a CSRF rejection.
    rv = csrf_client.post("/stripe/webhook", data=b"{}", content_type="application/json")
    assert not _is_csrf_reject(rv)


def test_api_auth_challenge_is_exempt(csrf_client):
    # Exempt (ECDSA signature auth): no token, must NOT be a CSRF rejection.
    rv = csrf_client.post("/api/auth/challenge", json={"user_id": "nobody"})
    assert not _is_csrf_reject(rv)


def test_csrf_off_by_default_under_test_runner():
    """Sanity: without CSRF_ENABLED the test runner disables CSRF, so unrelated
    POST tests don't need a token. Production (TESTING unset) still enforces."""
    _fresh_db()
    app.config["TESTING"] = True
    app.config.pop("CSRF_ENABLED", None)
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            rv = c.post(PROTECTED)          # no token
        db.session.remove()
        db.drop_all()
    assert not _is_csrf_reject(rv)
