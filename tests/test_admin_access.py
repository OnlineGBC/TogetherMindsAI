"""
tests/test_admin_access.py
--------------------------
Comp access (full access without paying) and the admin second-factor challenge
(authenticator app OR emailed code — either one is enough).

Two things these pin down that are easy to get wrong:
  * a comped email must be found even though Clinician.email is encrypted with a
    non-deterministic cipher (hence the HMAC lookup hash), and
  * a missing/unconfigured factor must never count as a pass.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-admin")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
import admin_access
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, CompAccess, AdminAuthCode

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"          # RFC 4648 base32, test-only


@pytest.fixture
def client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _admin_config():
    """Patch in a fully-configured admin console."""
    return [
        patch.object(config, "ADMIN_EMAILS", [ADMIN]),
        patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
        patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET),
    ]


def _with_admin_config(fn):
    """Run fn() with the admin console configured."""
    patches = _admin_config()
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


def _seed_clinician(uid, email):
    db.session.add(Clinician(id=uid, provider="google", provider_subject=uid,
                             email=email, created_at=datetime.now(timezone.utc)))
    db.session.commit()
    return db.session.get(Clinician, uid)


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------

def test_comped_email_gets_premium_while_billing_is_on(client):
    """The whole point: no Stripe subscription, but full access."""
    with app.app_context():
        clin = _seed_clinician("c1", "friend@example.com")
        assert clin.subscription_status is None          # never paid
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(clin) == "free"    # before the grant
            admin_access.grant(db, CompAccess, "friend@example.com", "beta tester", ADMIN)
            assert tm._effective_plan(clin) == "premium"
            assert tm._has_recording(clin) is True
            assert tm._has_ai_analysis(clin) is True


def test_comp_lookup_survives_encrypted_email(client):
    """Clinician.email is Fernet-encrypted (different ciphertext each time), so the
    match must come from the HMAC hash, not an equality query on the stored value."""
    with app.app_context():
        _seed_clinician("c2", "Mixed.Case@Example.COM ")
        admin_access.grant(db, CompAccess, "mixed.case@example.com", "", ADMIN)
        # Same address, different casing/spacing on the way in.
        assert admin_access.has_comp_access(CompAccess, "  MIXED.CASE@example.com ") is True


def test_revoked_comp_loses_access(client):
    with app.app_context():
        clin = _seed_clinician("c3", "expired@example.com")
        row = admin_access.grant(db, CompAccess, "expired@example.com", "", ADMIN)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(clin) == "premium"
            assert admin_access.revoke(db, CompAccess, row.id) is True
            assert tm._effective_plan(clin) == "free"
        # The row survives for audit rather than being deleted.
        assert db.session.get(CompAccess, row.id) is not None


def test_non_comped_email_unaffected(client):
    with app.app_context():
        clin = _seed_clinician("c4", "stranger@example.com")
        admin_access.grant(db, CompAccess, "someone.else@example.com", "", ADMIN)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(clin) == "free"


def test_comp_can_be_granted_before_the_person_signs_up(client):
    """Grant first, account created later — access applies at first login."""
    with app.app_context():
        admin_access.grant(db, CompAccess, "future@example.com", "pre-paid", ADMIN)
        clin = _seed_clinician("c5", "future@example.com")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(clin) == "premium"


# ---------------------------------------------------------------------------
# The second-factor challenge (either factor alone is enough)
# ---------------------------------------------------------------------------

def _totp_now():
    import pyotp
    return pyotp.TOTP(TOTP_SECRET).now()


def test_either_factor_alone_is_enough(client):
    with app.app_context():
        def check():
            # Emailed code on its own.
            email_code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            assert admin_access.challenge_passed(
                db, AdminAuthCode, ADMIN, email_code=email_code) is True
            # Authenticator app on its own.
            assert admin_access.challenge_passed(
                db, AdminAuthCode, ADMIN, totp=_totp_now()) is True
        _with_admin_config(check)


def test_totp_code_accepts_the_space_authenticator_apps_display(client):
    """Google Authenticator shows codes as "123 456". That inner space survives
    .strip(), so typing the code as displayed used to fail every time."""
    with app.app_context():
        def check():
            code = _totp_now()
            spaced = f"{code[:3]} {code[3:]}"
            assert admin_access.verify_totp(spaced) is True
            assert admin_access.verify_totp(f"{code[:3]}-{code[3:]}") is True
        _with_admin_config(check)


def test_emailed_code_also_tolerates_separators(client):
    with app.app_context():
        def check():
            code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            spaced = f"{code[:3]} {code[3:]}"
            assert admin_access.verify_code(
                db, AdminAuthCode, ADMIN, "email", spaced) is True
        _with_admin_config(check)


def test_normalise_code_strips_only_separators(client):
    assert admin_access.normalise_code(" 123 456 ") == "123456"
    assert admin_access.normalise_code("123-456") == "123456"
    assert admin_access.normalise_code("") == ""
    assert admin_access.normalise_code("abc") == ""          # nothing to verify


def test_no_factor_at_all_fails(client):
    """Being signed in as an admin is not by itself enough."""
    with app.app_context():
        def check():
            assert admin_access.challenge_passed(db, AdminAuthCode, ADMIN) is False
            assert admin_access.challenge_passed(
                db, AdminAuthCode, ADMIN, totp="000000", email_code="000000") is False
        _with_admin_config(check)


def test_totp_secret_survives_a_bom_and_crlf(client):
    """A secret piped in from a Windows shell arrives as BOM + value + CRLF.
    str.strip() removes the CRLF but NOT the BOM (U+FEFF is not whitespace), so the
    secret failed to base32-decode and the factor silently returned false."""
    dirty = "﻿" + TOTP_SECRET + "\r\n"
    with app.app_context():
        with patch.object(config, "ADMIN_TOTP_SECRET", dirty):
            assert admin_access.clean_secret(dirty) == TOTP_SECRET
            assert admin_access.verify_totp(_totp_now()) is True


def test_totp_secret_tolerates_display_spacing(client):
    """Some tools show a key grouped in fours; pasting that must still work."""
    spaced = " ".join(TOTP_SECRET[i:i + 4] for i in range(0, len(TOTP_SECRET), 4))
    with app.app_context():
        with patch.object(config, "ADMIN_TOTP_SECRET", spaced):
            assert admin_access.verify_totp(_totp_now()) is True


def test_unconfigured_totp_never_counts_as_a_factor(client):
    """A blank secret must fail closed, not silently pass."""
    with app.app_context():
        with patch.object(config, "ADMIN_TOTP_SECRET", ""):
            assert admin_access.verify_totp("123456") is False
            assert admin_access.verify_totp("") is False


def test_code_is_single_use(client):
    with app.app_context():
        def check():
            code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", code) is True
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", code) is False
        _with_admin_config(check)


def test_expired_code_rejected(client):
    with app.app_context():
        def check():
            code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            row = AdminAuthCode.query.order_by(AdminAuthCode.id.desc()).first()
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.session.commit()
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", code) is False
        _with_admin_config(check)


def test_code_attempts_are_capped(client):
    with app.app_context():
        def check():
            code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            for _ in range(config.ADMIN_CODE_MAX_ATTEMPTS):
                admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", "000000")
            # Budget spent — even the RIGHT code is now refused.
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", code) is False
        _with_admin_config(check)


def test_issuing_a_new_code_retires_the_previous_one(client):
    with app.app_context():
        def check():
            first = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            second = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", first) is False
            assert admin_access.verify_code(db, AdminAuthCode, ADMIN, "email", second) is True
        _with_admin_config(check)


# ---------------------------------------------------------------------------
# Route guards
# ---------------------------------------------------------------------------

def test_console_is_404_for_anonymous_and_for_a_normal_clinician(client):
    with app.app_context():
        _seed_clinician("c6", "normal@example.com")

    def check():
        assert client.get("/accessadmin").status_code == 404      # signed out
        with client.session_transaction() as s:
            s["user_id"] = "c6"
        assert client.get("/accessadmin").status_code == 404      # not an admin
    _with_admin_config(check)


def test_console_is_404_when_no_admin_is_configured(client):
    """An unconfigured deploy must not expose the console to anyone."""
    with app.app_context():
        _seed_clinician("c7", ADMIN)
    with patch.object(config, "ADMIN_EMAILS", []), \
         patch.object(config, "ADMIN_CONSOLE_ENABLED", False):
        with client.session_transaction() as s:
            s["user_id"] = "c7"
        assert client.get("/accessadmin").status_code == 404


def test_admin_sees_challenge_and_cannot_grant_until_verified(client):
    with app.app_context():
        _seed_clinician("c8", ADMIN)

    def check():
        with client.session_transaction() as s:
            s["user_id"] = "c8"
        rv = client.get("/accessadmin")
        assert rv.status_code == 200
        assert b"Confirm it's you" in rv.data                # challenge, not the console
        # Granting before verifying is refused outright.
        rv = client.post("/accessadmin/add",
                         data={"email": "sneaky@example.com", "note": ""})
        assert rv.status_code == 403
        with app.app_context():
            assert admin_access.has_comp_access(CompAccess, "sneaky@example.com") is False
    _with_admin_config(check)


def test_verified_admin_can_grant_and_revoke(client):
    with app.app_context():
        _seed_clinician("c9", ADMIN)

    def check():
        with client.session_transaction() as s:
            s["user_id"] = "c9"
        with app.app_context():
            code = admin_access.issue_code(db, AdminAuthCode, ADMIN, "email")
        rv = client.post("/accessadmin/verify",
                         data={"totp": _totp_now(), "email_code": code},
                         follow_redirects=True)
        assert rv.status_code == 200

        client.post("/accessadmin/add",
                    data={"email": "granted@example.com", "note": "friend"},
                    follow_redirects=True)
        with app.app_context():
            assert admin_access.has_comp_access(CompAccess, "granted@example.com") is True
            row = CompAccess.query.first()

        client.post("/accessadmin/revoke", data={"id": row.id}, follow_redirects=True)
        with app.app_context():
            assert admin_access.has_comp_access(CompAccess, "granted@example.com") is False
    _with_admin_config(check)


def test_wrong_codes_do_not_verify_the_session(client):
    with app.app_context():
        _seed_clinician("c10", ADMIN)

    def check():
        with client.session_transaction() as s:
            s["user_id"] = "c10"
        rv = client.post("/accessadmin/verify",
                         data={"totp": "000000", "email_code": "000000"},
                         follow_redirects=True)
        assert rv.status_code == 200
        assert b"Confirm it's you" in rv.data                 # still challenged
        rv = client.post("/accessadmin/add", data={"email": "no@example.com"})
        assert rv.status_code == 403
    _with_admin_config(check)


def test_sms_channel_is_gone(client):
    """SMS was removed — an sms code request must not be accepted."""
    with app.app_context():
        _seed_clinician("c11", ADMIN)
    assert "sms" not in admin_access.CHANNELS

    def check():
        with client.session_transaction() as s:
            s["user_id"] = "c11"
        rv = client.post("/accessadmin/send-code", data={"channel": "sms"})
        assert rv.status_code == 400
    _with_admin_config(check)
