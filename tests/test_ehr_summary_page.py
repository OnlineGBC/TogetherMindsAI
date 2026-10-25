"""
tests/test_ehr_summary_page.py
------------------------------
The readable AI summary page that a chart note links to, and the rule that anyone
who signed in through Epic or Oracle Health gets the full product.

The page is keyed by an opaque token (never the Session ID) and is for the
clinician who LED the session, and nobody else.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-summary-page")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import config
import TogetherMindsAI as _tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession

init_encryption(TEST_KEY)

TOKEN = "opaque-summary-token-123"
SESSION_ID = "SUMMARYSESSION001"


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


def _clinician(cid, provider="epic", **over):
    now = datetime.now(timezone.utc)
    values = dict(id=cid, provider=provider, provider_subject="sub-" + cid,
                  created_at=now, last_login_at=now, role="psychotherapist")
    values.update(over)
    row = Clinician(**values)
    db.session.add(row)
    db.session.commit()
    return row


def _session(therapist_id="doc-1"):
    ts = TherapySession(id=SESSION_ID, mode="solo", created_by=therapist_id,
                        created_at=datetime.now(timezone.utc), therapist_id=therapist_id,
                        friendly_name="TestEpic1", download_token=TOKEN)
    db.session.add(ts)
    db.session.commit()
    return ts


def _sign_in(client, cid):
    with client.session_transaction() as s:
        s["user_id"] = cid
        s["clinician_id"] = cid


PAYLOAD = {"session_id": SESSION_ID, "clinical": "The patient described low mood.",
           "codes_rationale": "F32.1 fits the low mood.", "codes": [{"label": "Depression", "code": "F32.1"}],
           "cpt_suggestion": {"code": "90834", "label": "Psychotherapy, 45 minutes"},
           "pos": {"code": "10", "label": "Telehealth Provided in Patient's Home"},
           "disclaimer": "Therapist-only. AI-generated decision support.", "cached": True}


# --- the page ---------------------------------------------------------------

def test_an_unknown_token_is_a_404(client):
    assert client.get("/session/summary/not-a-real-token").status_code == 404


def test_signed_out_goes_to_sign_in_and_back_to_the_same_page(client):
    _clinician("doc-1")
    _session()
    rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]
    assert "summary" in rv.headers["Location"]          # the return address is kept


def test_another_clinician_cannot_open_it(client):
    _clinician("doc-1")
    _clinician("doc-2")
    _session("doc-1")
    _sign_in(client, "doc-2")
    with patch.object(_tm, "_session_summary_payload", return_value=PAYLOAD) as gen:
        rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 403
    gen.assert_not_called()


def test_the_clinician_who_led_the_session_sees_the_full_summary(client):
    _clinician("doc-1")
    _session("doc-1")
    _sign_in(client, "doc-1")
    with patch.object(_tm, "_session_summary_payload", return_value=PAYLOAD):
        rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 200
    for expected in (b"The patient described low mood.", b"F32.1 fits the low mood.",
                     b"CPT 90834", b"Place of Service 10", b"TestEpic1"):
        assert expected in rv.data


def test_the_page_never_shows_the_session_id(client):
    _clinician("doc-1")
    _session("doc-1")
    _sign_in(client, "doc-1")
    payload = dict(PAYLOAD)
    payload.pop("session_id")
    with patch.object(_tm, "_session_summary_payload", return_value=payload):
        rv = client.get("/session/summary/" + TOKEN)
    assert SESSION_ID.encode() not in rv.data


def test_if_the_ai_write_up_failed_the_page_says_to_try_again(client):
    _clinician("doc-1")
    _session("doc-1")
    _sign_in(client, "doc-1")
    with patch.object(_tm, "_session_summary_payload", return_value=dict(PAYLOAD, clinical="")):
        rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 200
    assert b"could not be made just now" in rv.data


def test_an_account_without_the_ai_plan_sees_a_plain_notice_and_no_summary_is_made(client):
    _clinician("doc-1", provider="google", plan="free")
    _session("doc-1")
    _sign_in(client, "doc-1")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch.object(_tm, "_session_summary_payload", return_value=PAYLOAD) as gen:
        rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 200
    assert b"not part of this account" in rv.data
    gen.assert_not_called()


# --- full product for Epic / Oracle Health sign-ins -------------------------

@pytest.mark.parametrize("provider", ["epic", "oracle"])
def test_an_ehr_sign_in_gets_the_full_product_even_on_the_free_plan(client, provider):
    doc = _clinician("doc-1", provider=provider, plan="free", subscription_status=None)
    with patch.object(config, "BILLING_ENABLED", True):
        assert _tm._is_paid(doc) is True
        assert _tm._has_ai_analysis(doc) is True
        assert _tm._has_icd_codes(doc) is True


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_other_sign_ins_still_need_a_paid_plan(client, provider):
    doc = _clinician("doc-1", provider=provider, plan="free", subscription_status=None)
    with patch.object(config, "BILLING_ENABLED", True):
        assert _tm._is_paid(doc) is False


def test_the_summary_page_opens_for_an_epic_clinician_when_billing_is_on(client):
    _clinician("doc-1", provider="epic", plan="free")
    _session("doc-1")
    _sign_in(client, "doc-1")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch.object(_tm, "_session_summary_payload", return_value=PAYLOAD):
        rv = client.get("/session/summary/" + TOKEN)
    assert rv.status_code == 200
    assert b"The patient described low mood." in rv.data
