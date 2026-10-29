"""
tests/test_role_pricing.py
--------------------------
Step 4: price follows the role, and the plans page shows only that role's tier.

The property worth guarding hardest: the amount charged is decided server-side
from the account's role. A crafted request must not be able to ask to be charged
a different price, or to buy a tier meant for someone else.
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
os.environ.setdefault("SECRET_KEY", "test-secret-role-pricing")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import billing
import config
import roles
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, CompAccess
import admin_access

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


def _clinician(uid="doc", role=roles.PSYCHOTHERAPIST, plan="free", status=None,
               email=None):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid, role=role,
        email=email or f"{uid}@example.com",
        created_at=datetime.now(timezone.utc),
        plan=plan, subscription_status=status))
    db.session.commit()


def _login(client, uid="doc"):
    with client.session_transaction() as s:
        s["clinician_id"] = uid
        s["user_id"] = uid


# ---------------------------------------------------------------------------
# Which price each role is sold
# ---------------------------------------------------------------------------

def test_both_clinical_roles_share_one_price():
    """The difference between them is ICD codes and the licence check, not money."""
    assert roles.price_key(roles.PSYCHOTHERAPIST) == "STRIPE_PRICE_CLINICAL"
    assert roles.price_key(roles.HYPNOTHERAPIST) == "STRIPE_PRICE_CLINICAL"
    assert roles.price_label(roles.PSYCHOTHERAPIST) == "$16"
    assert roles.price_label(roles.HYPNOTHERAPIST) == "$16"


def test_caregivers_have_their_own_cheaper_price():
    assert roles.price_key(roles.CAREGIVER) == "STRIPE_PRICE_CAREGIVER"
    assert roles.price_label(roles.CAREGIVER) == "$9.99"


def test_price_for_role_reads_the_configured_id():
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clin"), \
         patch.object(config, "STRIPE_PRICE_CAREGIVER", "price_care"):
        assert billing.price_for_role(roles.PSYCHOTHERAPIST) == "price_clin"
        assert billing.price_for_role(roles.CAREGIVER) == "price_care"


# ---------------------------------------------------------------------------
# Checkout charges the role's price, whatever the request says
# ---------------------------------------------------------------------------

def test_checkout_uses_the_accounts_own_role(client):
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("billing.create_checkout_url", return_value="https://stripe.test/x") as mk:
        client.post("/billing/checkout/paid")
    assert mk.call_args.args[1] == roles.CAREGIVER


def test_a_crafted_request_cannot_pick_a_different_price(client):
    """The <plan> in the URL is ignored beyond a sanity check — the price comes
    from the server-side role, so this cannot be used to underpay."""
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("billing.create_checkout_url", return_value="https://stripe.test/x") as mk:
        client.post("/billing/checkout/paid")
    assert mk.call_args.args[1] == roles.PSYCHOTHERAPIST


def test_checkout_refuses_an_unknown_role():
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clin"):
        assert billing.create_checkout_url(object(), "wizard", "s", "c") is None


# ---------------------------------------------------------------------------
# The plans page shows one tier — theirs
# ---------------------------------------------------------------------------

def test_a_caregiver_sees_their_own_price_only(client):
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    body = client.get("/pricing").data.decode()
    assert "$9.99" in body
    assert "$16" not in body           # not shown someone else's tier


def test_a_coach_sees_the_clinical_price_but_no_icd_promise(client):
    with app.app_context():
        _clinician(role=roles.HYPNOTHERAPIST)
    _login(client)
    body = client.get("/pricing").data.decode()
    assert "$16" in body
    assert "ICD" not in body           # never advertised to a role that cannot have it


def test_a_psychotherapist_is_offered_icd_codes(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    body = client.get("/pricing").data.decode()
    assert "ICD and billing codes" in body


def test_a_caregiver_is_not_promised_chat_or_ai(client):
    """Caregiver mode has no words, so none of the AI features may be advertised."""
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    body = client.get("/pricing").data.decode()
    for promise in ("AI co-pilot suggestions", "AI session recap",
                    "Full session transcript", "Guided reflections chat"):
        assert promise not in body, promise


# ---------------------------------------------------------------------------
# A comped account must not be sold what it already has
# ---------------------------------------------------------------------------

def test_a_comped_account_is_told_so_and_not_asked_to_pay(client):
    """Before this, a comped account was shown "Free" and offered a subscription
    for things it already had."""
    with app.app_context():
        _clinician(email="friend@example.com")
        admin_access.grant(db, CompAccess, "friend@example.com", "", "admin@x")
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        body = client.get("/pricing").data.decode()
    assert "granted by your practice" in body
    assert "Subscribe" not in body          # nothing to buy
