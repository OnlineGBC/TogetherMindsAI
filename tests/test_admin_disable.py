"""
tests/test_admin_disable.py
---------------------------
Switching a clinician account off, and back on again, from /accessadmin.

Deliberately not deletion. An account's id is also written into their therapy
sessions and state licence certificates, which are client records the practice
has to retain — deleting the account would orphan them. Disabling takes away
access and destroys nothing, so it is reversible and needs no confirm step.
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
os.environ.setdefault("SECRET_KEY", "test-secret-admin-disable")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import admin_access
import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, AuditLog

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


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


def _seed(uid, email, role=roles.PSYCHOTHERAPIST, disabled=False):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid, email=email, role=role,
        created_at=datetime.now(timezone.utc),
        disabled_at=datetime.now(timezone.utc) if disabled else None,
    ))
    db.session.commit()


def _as_verified_admin(client, fn):
    """Run fn() signed in as an admin who has passed the second factor."""
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            _seed("admin", ADMIN)
        with client.session_transaction() as s:
            s["user_id"] = "admin"
        import pyotp
        client.post("/accessadmin/verify",
                    data={"totp": pyotp.TOTP(TOTP_SECRET).now()},
                    follow_redirects=True)
        return fn()
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def test_set_disabled_switches_off_and_back_on(client):
    with app.app_context():
        _seed("t1", "t1@example.com")
        assert admin_access.set_disabled(db, Clinician, "t1", True) is True
        assert db.session.get(Clinician, "t1").disabled_at is not None
        assert db.session.get(Clinician, "t1").is_disabled is True

        assert admin_access.set_disabled(db, Clinician, "t1", False) is False
        assert db.session.get(Clinician, "t1").disabled_at is None
        assert db.session.get(Clinician, "t1").is_disabled is False


def test_set_disabled_is_a_no_op_when_already_in_that_state(client):
    with app.app_context():
        _seed("t2", "t2@example.com")
        assert admin_access.set_disabled(db, Clinician, "t2", False) is None
        admin_access.set_disabled(db, Clinician, "t2", True)
        assert admin_access.set_disabled(db, Clinician, "t2", True) is None


def test_set_disabled_refuses_an_unknown_account(client):
    with app.app_context():
        assert admin_access.set_disabled(db, Clinician, "nope", True) is None


def test_disabling_deletes_nothing(client):
    """The whole point of disabling: the account row and its fields survive."""
    with app.app_context():
        _seed("t3", "keep@example.com", role=roles.CAREGIVER)
        admin_access.set_disabled(db, Clinician, "t3", True)
        row = db.session.get(Clinician, "t3")
        assert row is not None
        assert row.email == "keep@example.com"
        assert row.role == roles.CAREGIVER
        assert Clinician.query.count() == 1      # still there, just switched off


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def test_a_verified_admin_can_disable_and_enable(client):
    def check():
        with app.app_context():
            _seed("target", "target@example.com")

        rv = client.post("/accessadmin/disable",
                         data={"clinician_id": "target", "disabled": "1"},
                         follow_redirects=True)
        assert rv.status_code == 200
        with app.app_context():
            assert db.session.get(Clinician, "target").disabled_at is not None

        client.post("/accessadmin/disable",
                    data={"clinician_id": "target", "disabled": "0"},
                    follow_redirects=True)
        with app.app_context():
            assert db.session.get(Clinician, "target").disabled_at is None
    _as_verified_admin(client, check)


def test_the_change_is_written_to_the_audit_log_with_the_notice(client):
    def check():
        with app.app_context():
            _seed("target", "target@example.com")
        client.post("/accessadmin/disable",
                    data={"clinician_id": "target", "disabled": "1"},
                    follow_redirects=True)
        with app.app_context():
            row = (AuditLog.query
                   .filter_by(event_type="account_disabled").first())
            assert row is not None
            assert "target" in row.details
            # The stored wording is the same text the console shows.
            assert admin_access.DISABLE_NOTICE[:40] in row.details
    _as_verified_admin(client, check)


def test_an_admin_cannot_disable_their_own_account(client):
    def check():
        rv = client.post("/accessadmin/disable",
                         data={"clinician_id": "admin", "disabled": "1"},
                         follow_redirects=True)
        assert rv.status_code == 200
        with app.app_context():
            assert db.session.get(Clinician, "admin").disabled_at is None
    _as_verified_admin(client, check)


def test_an_unverified_admin_cannot_disable(client):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            _seed("admin", ADMIN)
            _seed("target", "target@example.com")
        with client.session_transaction() as s:
            s["user_id"] = "admin"          # signed in, second factor NOT passed
        rv = client.post("/accessadmin/disable",
                         data={"clinician_id": "target", "disabled": "1"})
        assert rv.status_code == 403
        with app.app_context():
            assert db.session.get(Clinician, "target").disabled_at is None
    finally:
        for p in patches:
            p.stop()


def test_the_console_shows_the_notice_and_the_status(client):
    def check():
        with app.app_context():
            _seed("on", "active@example.com")
            _seed("off", "switched@example.com", disabled=True)
        html = client.get("/accessadmin").get_data(as_text=True)
        assert admin_access.DISABLE_NOTICE[:40] in html
        assert "Disabled" in html and "Active" in html
        assert ">Disable<" in html and ">Enable<" in html
    _as_verified_admin(client, check)


def test_the_admins_own_row_has_no_disable_button(client):
    def check():
        html = client.get("/accessadmin").get_data(as_text=True)
        # The admin is the only account seeded, so no switch may appear at all.
        assert ">Disable<" not in html
    _as_verified_admin(client, check)


def test_your_own_row_says_You_rather_than_leaving_a_gap(client):
    """The missing button left an empty space that read as if it meant
    something — it only ever meant "this row is you"."""
    def check():
        html = client.get("/accessadmin").get_data(as_text=True)
        assert ">You</span>" in html
        assert "You cannot disable your own account." in html
    _as_verified_admin(client, check)


def test_every_row_ends_in_the_same_fixed_width_slot(client):
    """Regression: the row-ending control was sized to match its neighbours by
    eye. "Disable" is a longer word than "Enable", and a button carries border
    and padding that plain text does not, so all three widths differed and
    dragged the dropdown and Set to three different positions."""
    def check():
        with app.app_context():
            _seed("on", "active@example.com")
            _seed("off", "switched@example.com", disabled=True)
        html = client.get("/accessadmin").get_data(as_text=True)
        # One slot per account row: Disable, Enable and You alike.
        assert html.count('<div class="comp-switch">') == 3
        assert ".comp-table .comp-switch" in html      # the rule that fixes it
        # The old approach sized the label on its own; it must not come back.
        assert "min-width: 84px" not in html
    _as_verified_admin(client, check)


def test_admin_accounts_are_labelled_admin(client):
    """Written plainly in the Status column, and true of every admin account —
    a second admin keeps their Disable button but is still labelled."""
    def check():
        with app.app_context():
            _seed("plain", "notanadmin@example.com")
        html = client.get("/accessadmin").get_data(as_text=True)
        assert ">Admin</span>" in html
        # One pill only: the seeded non-admin account must not get one.
        assert html.count(">Admin</span>") == 1
    _as_verified_admin(client, check)


# ---------------------------------------------------------------------------
# The block itself
# ---------------------------------------------------------------------------

def test_a_disabled_clinician_loses_an_open_session(client):
    """Blocking at sign-in is not enough — someone already signed in when you
    disable them would otherwise keep working until they logged out."""
    with app.app_context():
        _seed("live", "live@example.com", disabled=True)
        target = app.url_map.bind("localhost").build("therapist_start")
    with client.session_transaction() as s:
        s["user_id"] = "live"
        s["clinician_id"] = "live"

    rv = client.get(target)
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]
    with client.session_transaction() as s:
        assert "clinician_id" not in s      # the session was cleared


def test_an_active_clinician_is_not_blocked(client):
    with app.app_context():
        _seed("ok", "ok@example.com")
        target = app.url_map.bind("localhost").build("therapist_start")
    with client.session_transaction() as s:
        s["user_id"] = "ok"
        s["clinician_id"] = "ok"

    rv = client.get(target)
    assert rv.status_code != 302 or "/login" not in rv.headers.get("Location", "")


def test_a_disabled_clinician_gets_json_on_an_api_route(client):
    with app.app_context():
        _seed("api", "api@example.com", disabled=True)
    with client.session_transaction() as s:
        s["user_id"] = "api"
        s["clinician_id"] = "api"

    # A real API endpoint: an unknown URL has no endpoint at all, so the
    # guard correctly stays out of the way and Flask just 404s.
    rv = client.post("/api/display-name", json={})
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "account_disabled"


def test_logout_still_works_when_disabled(client):
    """A blocked account must still be able to end its own session."""
    with app.app_context():
        _seed("out", "out@example.com", disabled=True)
    with client.session_transaction() as s:
        s["user_id"] = "out"
        s["clinician_id"] = "out"

    rv = client.get("/logout")
    assert rv.status_code in (200, 302)


def test_each_row_says_which_provider_and_when_they_last_signed_in(client):
    """Microsoft accounts hand us no email, so a row could read only
    "no email yet · cfc74874". A switched-off Microsoft account then looked
    unrelated to the person who could not sign in, and finding the cause needed
    a deployed log line. The provider and last sign-in identify the row."""
    def check():
        with app.app_context():
            row = Clinician(id="ms-1", provider="microsoft",
                            provider_subject="AAAA", email=None,
                            role=roles.HYPNOTHERAPIST,
                            created_at=datetime.now(timezone.utc),
                            last_login_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
            db.session.add(row)
            db.session.commit()
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "Microsoft" in html
        assert "last in 12 Aug 2026" in html
    _as_verified_admin(client, check)


def test_an_account_that_never_signed_in_says_so(client):
    def check():
        with app.app_context():
            db.session.add(Clinician(id="new-1", provider="google",
                                     provider_subject="s1", email="new@example.com",
                                     role=roles.PSYCHOTHERAPIST,
                                     created_at=datetime.now(timezone.utc),
                                     last_login_at=None))
            db.session.commit()
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "never signed in" in html
    _as_verified_admin(client, check)


def test_a_failed_sign_in_does_not_leave_you_signed_in_as_someone_else(client):
    """A sign-in that fails half way must clear the session. Otherwise the next
    page load acts on the OLD identity — and if that account is switched off, the
    message reads as if it were about the account you were trying to reach."""
    with app.app_context():
        _seed("old", "old@example.com", disabled=True)
    with client.session_transaction() as s:
        s["user_id"] = "old"
        s["clinician_id"] = "old"

    # The provider hands back nothing usable, so the callback bails out.
    with patch.object(app.view_functions["oauth_callback"].__globals__["_tm"],
                      "_oauth_userinfo", return_value=None):
        client.get("/auth/google/callback", follow_redirects=False)

    with client.session_transaction() as s:
        assert "clinician_id" not in s
        assert "user_id" not in s
