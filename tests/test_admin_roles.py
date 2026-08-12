"""
tests/test_admin_roles.py
-------------------------
Step 9: an admin can change someone's role from /accessadmin.

Deliberately not self-serve. A role decides what the app may claim and store
about someone's work, so moving one can take away ICD codes or remove the state
licence check from their sessions.
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
os.environ.setdefault("SECRET_KEY", "test-secret-admin-roles")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import admin_access
import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, CompAccess

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


def _seed(uid, email, role=roles.PSYCHOTHERAPIST):
    db.session.add(Clinician(id=uid, provider="google", provider_subject=uid,
                             email=email, role=role,
                             created_at=datetime.now(timezone.utc)))
    db.session.commit()


def _admin_patches():
    return [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
            patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
            patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]


def _as_verified_admin(client, fn):
    """Run fn() signed in as an admin who has passed the second factor."""
    patches = _admin_patches()
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
# The change itself
# ---------------------------------------------------------------------------

def test_set_role_changes_it(client):
    with app.app_context():
        _seed("target", "coach@example.com", role=roles.PSYCHOTHERAPIST)
        out = admin_access.set_role(db, Clinician, "target", roles.HYPNOTHERAPIST)
        assert out == (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST)
        assert db.session.get(Clinician, "target").role == roles.HYPNOTHERAPIST


def test_set_role_refuses_a_junk_role(client):
    with app.app_context():
        _seed("target", "x@example.com")
        assert admin_access.set_role(db, Clinician, "target", "wizard") is None
        assert db.session.get(Clinician, "target").role == roles.PSYCHOTHERAPIST


def test_set_role_refuses_an_unknown_account(client):
    with app.app_context():
        assert admin_access.set_role(db, Clinician, "nobody", roles.CAREGIVER) is None


def test_set_role_is_a_no_op_when_already_that_role(client):
    """Reports no change, so the console does not claim it did something."""
    with app.app_context():
        _seed("target", "x@example.com", role=roles.CAREGIVER)
        assert admin_access.set_role(db, Clinician, "target", roles.CAREGIVER) is None


# ---------------------------------------------------------------------------
# The account list
# ---------------------------------------------------------------------------

def test_account_list_reports_the_true_total(client):
    """It must say how many exist, not just how many are shown. Silent truncation
    on an admin screen reads as "this is all of them"."""
    with app.app_context():
        for i in range(5):
            _seed(f"c{i}", f"c{i}@example.com")
        rows, total = admin_access.list_accounts(Clinician, limit=2)
        assert len(rows) == 2
        assert total == 5


# ---------------------------------------------------------------------------
# Only a verified admin may do it
# ---------------------------------------------------------------------------

def test_a_normal_clinician_cannot_change_a_role(client):
    with app.app_context():
        _seed("someone", "someone@example.com")
        _seed("target", "target@example.com")
    patches = _admin_patches()
    for p in patches:
        p.start()
    try:
        with client.session_transaction() as s:
            s["user_id"] = "someone"          # signed in, but not an admin
        rv = client.post("/accessadmin/role",
                         data={"clinician_id": "target", "role": roles.CAREGIVER})
        assert rv.status_code == 404          # console does not exist for them
        with app.app_context():
            assert db.session.get(Clinician, "target").role == roles.PSYCHOTHERAPIST
    finally:
        for p in patches:
            p.stop()


def test_an_admin_who_has_not_passed_the_second_factor_cannot(client):
    with app.app_context():
        _seed("admin", ADMIN)
        _seed("target", "target@example.com")
    patches = _admin_patches()
    for p in patches:
        p.start()
    try:
        with client.session_transaction() as s:
            s["user_id"] = "admin"            # admin, but not verified
        rv = client.post("/accessadmin/role",
                         data={"clinician_id": "target", "role": roles.CAREGIVER})
        assert rv.status_code == 403
        with app.app_context():
            assert db.session.get(Clinician, "target").role == roles.PSYCHOTHERAPIST
    finally:
        for p in patches:
            p.stop()


def test_a_verified_admin_can_change_a_role_through_the_console(client):
    def check():
        with app.app_context():
            _seed("target", "target@example.com", role=roles.PSYCHOTHERAPIST)
        rv = client.post("/accessadmin/role",
                         data={"clinician_id": "target", "role": roles.CAREGIVER},
                         follow_redirects=True)
        assert rv.status_code == 200
        with app.app_context():
            assert db.session.get(Clinician, "target").role == roles.CAREGIVER
    _as_verified_admin(client, check)


def test_the_console_lists_accounts_with_a_role_selector(client):
    def check():
        with app.app_context():
            _seed("target", "listed@example.com", role=roles.HYPNOTHERAPIST)
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "listed@example.com" in html
        assert "Change someone&#39;s role" in html or "Change someone's role" in html
        for _value, label, _blurb in roles.choices():
            assert label in html
    _as_verified_admin(client, check)


def test_a_flash_message_is_printed_once(client):
    """base.html prints flashed messages for every page. This template printed
    them a second time, so every message appeared twice on screen."""
    def check():
        with app.app_context():
            _seed("target", "listed@example.com", role=roles.HYPNOTHERAPIST)
        html = client.post("/accessadmin/role",
                           data={"clinician_id": "target",
                                 "role": roles.CAREGIVER},
                           follow_redirects=True).get_data(as_text=True)
        assert html.count("Role changed to") == 1
    _as_verified_admin(client, check)


def test_the_row_actions_look_clickable(client):
    """Set used the outline style, which on this dark theme has a dim border and
    grey text — it read as a label, not a button. It is now solid blue like Add,
    and the action column carries a heading so the column is labelled."""
    def check():
        with app.app_context():
            _seed("target", "listed@example.com", role=roles.HYPNOTHERAPIST)
            # Seed a grant too: the Current grants table (and so its heading)
            # only renders when at least one grant exists.
            admin_access.grant(db, CompAccess, "granted@example.com", "note", ADMIN)
        html = client.get("/accessadmin").get_data(as_text=True)
        assert 'class="btn btn-primary btn-sm rounded-pill" type="submit">Set<' in html
        assert "btn-outline-secondary btn-sm rounded-pill\" type=\"submit\">Set<" not in html
        # Both tables label their action column (roles, and current grants).
        assert html.count('<th class="text-end">Action</th>') == 2
    _as_verified_admin(client, check)


def test_an_account_without_an_email_shows_a_hint_not_a_raw_uuid(client):
    """Accounts keep no email until their owner next logs in. The console used to
    fall back to the 36-character UUID, which read as gibberish in the table."""
    uid = "cfc74874-596b-41ca-909b-16d90a71b2fb"

    def check():
        with app.app_context():
            _seed(uid, None, role=roles.HYPNOTHERAPIST)
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "no email yet" in html
        assert uid[:8] in html            # enough to tell two such rows apart
        assert ">" + uid + "<" not in html   # never the whole UUID as the label
        # The role form still posts the full id, so Set keeps working.
        assert 'value="{}"'.format(uid) in html
    _as_verified_admin(client, check)
