"""
tests/test_role_gates.py
------------------------
Step 2 of the roles work: the role actually gating things.

Two rules are being enforced here, and both are easy to get wrong:

  * ICD coding belongs to paid psychotherapists ALONE. Paying does not buy it for
    a coach, and asking the co-pilot for codes must not get round it either.
  * The state licence check is for licensed clinical work. A coach or caregiver
    holds no such licence, so the gate must not apply — if it did, their clients
    would wait forever on a certification that never comes.
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
os.environ.setdefault("SECRET_KEY", "test-secret-role-gates")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import config
import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession, CopilotCard

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


def _clinician(uid="doc", role=roles.PSYCHOTHERAPIST, plan="premium", status="active"):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid,
        email=f"{uid}@example.com", created_at=datetime.now(timezone.utc),
        role=role, plan=plan, subscription_status=status))
    db.session.commit()
    return db.session.get(Clinician, uid)


def _session(sid="s1", therapist="doc"):
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by=therapist, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()


# ---------------------------------------------------------------------------
# ICD codes: paid psychotherapists only
# ---------------------------------------------------------------------------

def test_paid_psychotherapist_gets_icd_codes(client):
    with app.app_context():
        c = _clinician(role=roles.PSYCHOTHERAPIST)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._has_ai_analysis(c) is True
            assert tm._has_icd_codes(c) is True


def test_paying_does_not_buy_icd_codes_for_a_coach(client):
    """The whole point of the role split: a coach can pay for everything their
    plan offers and still never get clinical coding."""
    with app.app_context():
        c = _clinician(role=roles.HYPNOTHERAPIST)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._has_ai_analysis(c) is True     # they DO get the co-pilot
            assert tm._has_icd_codes(c) is False      # but never the codes


def test_free_psychotherapist_gets_no_icd_codes(client):
    with app.app_context():
        c = _clinician(role=roles.PSYCHOTHERAPIST, plan="free", status=None)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._has_icd_codes(c) is False


def test_caregiver_gets_no_ai_at_all_even_when_paid(client):
    with app.app_context():
        c = _clinician(role=roles.CAREGIVER)
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._has_ai_analysis(c) is False
            assert tm._has_icd_codes(c) is False
            assert tm._has_recording(c) is True        # recording is what they buy


# ---------------------------------------------------------------------------
# ICD codes must not leak through the co-pilot or the documents
# ---------------------------------------------------------------------------

def test_reference_cards_are_not_generated_for_a_coach(client):
    """Reference cards ARE the codes. A coach gets suggestions but no references."""
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
        with patch.object(config, "BILLING_ENABLED", True), \
             patch("copilot.build_reference_cards", return_value=[]) as refs, \
             patch("copilot.generate_suggestions", return_value=[]) as sugg:
            tm._run_copilot("s1", "solo")
        refs.assert_not_called()
        sugg.assert_called_once()


def test_reference_cards_are_generated_for_a_paid_psychotherapist(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
        with patch.object(config, "BILLING_ENABLED", True), \
             patch("copilot.build_reference_cards", return_value=[]) as refs, \
             patch("copilot.generate_suggestions", return_value=[]):
            tm._run_copilot("s1", "solo")
        refs.assert_called_once()


def test_codes_banked_under_an_old_role_stop_appearing(client):
    """A role can change. Codes stored while they were a psychotherapist must not
    keep showing up in documents after they become a coach."""
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
        db.session.add(CopilotCard(
            session_id="s1", card_type="reference",
            text="This is associated with anxiety.",
            payload='{"code": "6B00", "source": "ICD-11"}'))
        db.session.add(CopilotCard(
            session_id="s1", card_type="observation", text="Repeated deflection.",
            payload='{}'))
        db.session.commit()

        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._surfaced_codes("s1") == []
            cards = tm._session_copilot_cards("s1")
        # The reference card is dropped entirely; the observation survives.
        assert [c["type"] for c in cards] == ["observation"]


def test_codes_still_appear_for_a_paid_psychotherapist(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
        db.session.add(CopilotCard(
            session_id="s1", card_type="reference",
            text="This is associated with anxiety.",
            payload='{"code": "6B00", "source": "ICD-11"}'))
        db.session.commit()
        with patch.object(config, "BILLING_ENABLED", True):
            assert [c["code"] for c in tm._surfaced_codes("s1")] == ["6B00"]


def test_copilot_reply_is_told_not_to_offer_codes_to_a_coach(client):
    """The reply prompt invites ICD suggestions when asked. Without this a coach
    could simply ask for codes and be given them."""
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
        with patch.object(config, "BILLING_ENABLED", True), \
             patch("copilot.answer_therapist", return_value="") as answer:
            tm._answer_therapist_note("s1", "doc", "give me billing codes")
        assert answer.call_args.kwargs["allow_icd"] is False


def test_copilot_reply_allows_codes_for_a_paid_psychotherapist(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
        with patch.object(config, "BILLING_ENABLED", True), \
             patch("copilot.answer_therapist", return_value="") as answer:
            tm._answer_therapist_note("s1", "doc", "give me billing codes")
        assert answer.call_args.kwargs["allow_icd"] is True


def test_no_icd_rule_is_added_to_the_prompt_when_codes_are_off():
    """The rule has to reach the model, not just the function signature."""
    import copilot
    with patch("copilot._get_claude_client") as mk:
        mk.return_value.messages.create.return_value.content = [type("T", (), {"text": "ok"})()]
        copilot.answer_therapist("q", allow_icd=False)
        blocked = mk.return_value.messages.create.call_args.kwargs["system"]
        copilot.answer_therapist("q", allow_icd=True)
        allowed = mk.return_value.messages.create.call_args.kwargs["system"]
    assert "Never offer ICD or DSM codes" in blocked
    assert "Never offer ICD or DSM codes" not in allowed


# ---------------------------------------------------------------------------
# The state licence check
# ---------------------------------------------------------------------------

def test_licence_gate_applies_to_psychotherapists_only(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
        assert tm._licence_gate_applies("s1") is True

        db.session.get(Clinician, "doc").role = roles.HYPNOTHERAPIST
        db.session.commit()
        assert tm._licence_gate_applies("s1") is False

        db.session.get(Clinician, "doc").role = roles.CAREGIVER
        db.session.commit()
        assert tm._licence_gate_applies("s1") is False


def test_client_of_a_coach_is_admitted_without_a_state_certification(client):
    """With the gate off, a consented client goes straight in. If the gate still
    applied they would wait forever for a certification that never comes."""
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
        s["consented_sessions"] = ["s1"]
    rv = client.get("/therapy/solo/s1")
    assert rv.status_code == 200          # rendered, not bounced to the state gate


def test_client_of_a_psychotherapist_still_hits_the_state_gate(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
        s["consented_sessions"] = ["s1"]
    rv = client.get("/therapy/solo/s1")
    assert rv.status_code in (301, 302)
    assert "/state-gate" in rv.headers["Location"]


def test_consent_page_hides_the_location_question_for_a_coach(client):
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
    html = client.get("/session/s1/consent").get_data(as_text=True)
    assert "stateSelect" not in html
    assert "accurately stated my current location" not in html


def test_consent_page_shows_the_location_question_for_a_psychotherapist(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
        _session()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
    html = client.get("/session/s1/consent").get_data(as_text=True)
    assert "stateSelect" in html


def test_consent_without_a_location_admits_a_coachs_client(client):
    """The fields are gone from the form, so the POST arrives with no location.
    It must be accepted, not bounced back with 'please select your location'."""
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
        _session()
    with client.session_transaction() as s:
        s["user_id"] = "client-1"
    rv = client.post("/session/s1/consent", data={})
    assert rv.status_code in (301, 302)
    assert "/therapy/solo/s1" in rv.headers["Location"]
