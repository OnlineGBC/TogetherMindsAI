"""
tests/test_record_authorisation.py
----------------------------------
Step 8: a caregiver must confirm they are authorised to record the person.

The person being recorded — a baby, a patient — often cannot consent for
themselves, so the caregiver attests they hold the authority instead. The other
roles keep the all-party consent flow, where participants speak for themselves.

Stored rather than kept in the browser session: it is a legal attestation, so who
confirmed it and when has to survive a sign-out and a restart.
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
os.environ.setdefault("SECRET_KEY", "test-secret-recordauth")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import (db, init_encryption, Clinician, TherapySession,
                    RecordAuthorisation)

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


def _setup(role=roles.CAREGIVER, plan="paid"):
    now = datetime.now(timezone.utc)
    db.session.add(Clinician(id="doc", provider="google", provider_subject="doc",
                             email="doc@example.com", role=role, plan=plan,
                             subscription_status="active", created_at=now))
    db.session.add(TherapySession(
        id="s1", mode="solo", created_by="doc", created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id="doc"))
    db.session.commit()


def _login(client):
    with client.session_transaction() as s:
        s["user_id"] = "doc"
        s["clinician_id"] = "doc"


# ---------------------------------------------------------------------------
# Who has to confirm
# ---------------------------------------------------------------------------

def test_a_caregiver_must_confirm_first(client):
    with app.app_context():
        _setup(role=roles.CAREGIVER)
        assert tm._needs_record_authorisation("s1") is True


def test_other_roles_are_never_asked(client):
    """They use the all-party consent flow, where people speak for themselves."""
    for role in (roles.PSYCHOTHERAPIST, roles.HYPNOTHERAPIST):
        with app.app_context():
            db.session.query(Clinician).delete()
            db.session.query(TherapySession).delete()
            db.session.commit()
            _setup(role=role)
            assert tm._needs_record_authorisation("s1") is False


def test_confirming_once_is_enough(client):
    with app.app_context():
        _setup()
    _login(client)
    client.post("/session/s1/record-authorise", data={"authorised": "1"})
    with app.app_context():
        assert tm._needs_record_authorisation("s1") is False


# ---------------------------------------------------------------------------
# Recording is refused until it is confirmed
# ---------------------------------------------------------------------------

def test_recording_is_refused_before_confirming(client):
    with app.app_context():
        _setup()
    _login(client)
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(config, "BILLING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG") as start:
        rv = client.post("/session/s1/recording/start")
    assert rv.status_code == 403
    assert rv.get_json()["error"] == "authorisation_required"
    start.assert_not_called()


def test_recording_is_allowed_after_confirming(client):
    with app.app_context():
        _setup()
    _login(client)
    client.post("/session/s1/record-authorise", data={"authorised": "1"})
    with patch.object(config, "RECORDING_ENABLED", True), \
         patch.object(config, "BILLING_ENABLED", True), \
         patch("recording.start_recording", return_value="EG"), \
         patch("recording.stop_recording", return_value=True), \
         patch.object(tm, "_dispatch_recording_ready"):
        rv = client.post("/session/s1/recording/start")
    assert rv.status_code == 200


# ---------------------------------------------------------------------------
# The attestation itself
# ---------------------------------------------------------------------------

def test_an_unticked_box_confirms_nothing(client):
    """The checkbox is `required` in the browser, but the server must not rely
    on that — a POST without it must not create the record."""
    with app.app_context():
        _setup()
    _login(client)
    client.post("/session/s1/record-authorise", data={})
    with app.app_context():
        assert RecordAuthorisation.query.count() == 0
        assert tm._needs_record_authorisation("s1") is True


def test_only_the_sessions_own_practitioner_can_confirm(client):
    with app.app_context():
        _setup()
    with client.session_transaction() as s:
        s["user_id"] = "someone-else"
    rv = client.post("/session/s1/record-authorise", data={"authorised": "1"})
    assert rv.status_code == 403
    with app.app_context():
        assert RecordAuthorisation.query.count() == 0


def test_the_record_says_who_and_when(client):
    """It is an attestation, so it has to be auditable."""
    with app.app_context():
        _setup()
    _login(client)
    client.post("/session/s1/record-authorise", data={"authorised": "1"})
    with app.app_context():
        row = RecordAuthorisation.query.filter_by(session_id="s1").first()
        assert row.clinician_id == "doc"
        assert row.confirmed_at is not None


def test_the_room_shows_the_caregiver_wording(client):
    with app.app_context():
        _setup()
    _login(client)
    with patch.object(config, "RTC_ENABLED", True), \
         patch.object(config, "RECORDING_ENABLED", True):
        html = client.get("/therapy/solo/s1").get_data(as_text=True)
    assert "authorised to record this person" in html
    assert "Recording stays off until you confirm" in html
