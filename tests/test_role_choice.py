"""
tests/test_role_choice.py
-------------------------
Step 3 of the roles work: a clinician must choose a role before using the app.

The gate has to be strict enough to be unavoidable, and loose enough that it
cannot strand anybody. The awkward cases are the ones tested hardest: signing
out, reading the legal pages, and API callers who cannot follow a redirect.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-role-choice")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician

init_encryption(TEST_KEY)


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


def _clinician(uid="doc", role=None):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid,
        email=f"{uid}@example.com", created_at=datetime.now(timezone.utc), role=role))
    db.session.commit()


def _login(client, uid="doc"):
    with client.session_transaction() as s:
        s["user_id"] = uid
        s["clinician_id"] = uid


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_clinician_without_a_role_becomes_a_caregiver_without_being_asked(client):
    """Baby cam: no picker. The first request gives the account the caregiver role
    and carries on to the page they asked for."""
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.get("/therapist")
    assert rv.status_code == 200
    with app.app_context():
        assert db.session.get(Clinician, "doc").role == roles.CAREGIVER


def test_the_automatic_role_is_written_to_the_audit_log(client):
    from models import AuditLog
    with app.app_context():
        _clinician(role=None)
    _login(client)
    client.get("/therapist")
    with app.app_context():
        assert AuditLog.query.filter_by(event_type="role_chosen").count() == 1


def test_an_existing_role_is_never_overwritten_by_the_automatic_one(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    client.get("/therapist")
    with app.app_context():
        assert db.session.get(Clinician, "doc").role == roles.PSYCHOTHERAPIST


def test_clinician_with_a_role_is_not_asked(client):
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
    _login(client)
    assert client.get("/therapist").status_code == 200


def test_signed_out_visitors_are_not_asked(client):
    """The gate keys on being a signed-in clinician, not on the page."""
    assert client.get("/welcome").status_code == 200


def test_a_client_account_is_never_asked(client):
    """A role describes the practitioner, not the person they are seeing."""
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
        s["client_account_id"] = "client-1"
    assert client.get("/welcome").status_code == 200


# ---------------------------------------------------------------------------
# Not being stranded
# ---------------------------------------------------------------------------

def test_they_can_still_sign_out(client):
    """Otherwise a stuck account has no way out but clearing cookies."""
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.get("/logout")
    assert rv.status_code in (301, 302)
    assert "/choose-role" not in rv.headers["Location"]


def test_they_can_still_read_the_legal_pages(client):
    with app.app_context():
        _clinician(role=None)
    _login(client)
    for path in ("/privacy", "/tos"):
        assert client.get(path).status_code == 200, path


def test_the_picker_itself_is_reachable(client):
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.get("/choose-role")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    # Baby cam: the only choice is the caregiver, sent as a hidden value.
    assert 'name="role" value="caregiver"' in body
    for value in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        assert f'value="{value}"' not in body, value


def test_api_callers_with_no_role_get_the_caregiver_role_not_an_error(client):
    """A fetch() used to get a 403 role_required; now the role is set and the call
    goes through."""
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.post("/api/display-name", json={"name": "x"})
    assert rv.status_code != 403 or rv.get_json().get("error") != "role_required"
    with app.app_context():
        assert db.session.get(Clinician, "doc").role == roles.CAREGIVER


# ---------------------------------------------------------------------------
# Saving the choice
# ---------------------------------------------------------------------------

def test_choosing_a_role_saves_it_and_lets_them_through(client):
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.post("/choose-role", data={"role": roles.CAREGIVER, "next": "/therapist"})
    assert rv.status_code in (301, 302)
    assert "/therapist" in rv.headers["Location"]
    with app.app_context():
        assert db.session.get(Clinician, "doc").role == roles.CAREGIVER
    assert client.get("/therapist").status_code == 200      # gate no longer bites


def test_a_junk_role_is_refused(client):
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.post("/choose-role", data={"role": "wizard"})
    assert rv.status_code in (301, 302)
    assert "/choose-role" in rv.headers["Location"]
    with app.app_context():
        assert db.session.get(Clinician, "doc").role is None


def test_an_existing_role_is_never_overwritten(client):
    """Changing a role moves what the app may claim about their work, so it is an
    admin action. Re-posting here must not become a self-serve switch."""
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    client.post("/choose-role", data={"role": roles.CAREGIVER})
    with app.app_context():
        assert db.session.get(Clinician, "doc").role == roles.PSYCHOTHERAPIST


def test_the_next_target_cannot_be_an_outside_site(client):
    """next= is attacker-controllable, so it must not become an open redirect."""
    with app.app_context():
        _clinician(role=None)
    _login(client)
    rv = client.post("/choose-role",
                     data={"role": roles.HYPNOTHERAPIST, "next": "https://evil.example.com/x"})
    assert "evil.example.com" not in rv.headers["Location"]
