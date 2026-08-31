"""
tests/test_hours_topup.py
-------------------------
Buying another 40 recording hours.

The property that matters most: a top-up is a SINGLE charge. If the webhook
treated it like a subscription, someone buying extra hours would quietly be put
on a monthly plan they never asked for — and would be charged again next month.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-topup")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import billing
import config
import hours
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, HoursGrant

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


def _clinician(uid="care", role=roles.CAREGIVER, plan="paid", status="active",
               customer="cus_1"):
    db.session.add(Clinician(
        id=uid, provider="google", provider_subject=uid, role=role,
        email=f"{uid}@example.com", created_at=datetime.now(timezone.utc),
        plan=plan, subscription_status=status, stripe_customer_id=customer))
    db.session.commit()


def _login(client, uid="care"):
    with client.session_transaction() as s:
        s["clinician_id"] = uid
        s["user_id"] = uid


# ---------------------------------------------------------------------------
# It must be a one-off charge
# ---------------------------------------------------------------------------

def test_topup_checkout_is_a_one_time_payment_not_a_subscription():
    fake = MagicMock()
    fake.checkout.Session.create.return_value.url = "https://stripe.test/x"
    with patch.object(config, "STRIPE_PRICE_HOURS_TOPUP", "price_topup"), \
         patch("billing._init", return_value=fake), \
         patch("billing.ensure_customer", return_value="cus_1"):
        billing.create_topup_checkout_url(MagicMock(id="care"), "s", "c")
    kwargs = fake.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "payment"          # NOT "subscription"
    assert kwargs["metadata"]["kind"] == billing.TOPUP_KIND


def test_a_topup_webhook_credits_hours_and_does_not_grant_a_plan(client):
    """The important one. Treating this as a subscription would charge them
    monthly for something they bought once."""
    with app.app_context():
        _clinician(plan="free", status=None)
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1", "client_reference_id": "care",
                            "payment_intent": "pi_top_1",
                            "metadata": {"clinician_id": "care",
                                         "kind": billing.TOPUP_KIND}}},
    }
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        clin = db.session.get(Clinician, "care")
        assert clin.plan == "free"                    # NOT upgraded
        assert clin.subscription_status is None
        assert hours.remaining_minutes(HoursGrant, "care") == 40 * 60


def test_a_repeated_topup_webhook_credits_only_once(client):
    with app.app_context():
        _clinician()
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1", "client_reference_id": "care",
                            "payment_intent": "pi_same",
                            "metadata": {"clinician_id": "care",
                                         "kind": billing.TOPUP_KIND}}},
    }
    with patch("billing.verify_webhook", return_value=event):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert hours.remaining_minutes(HoursGrant, "care") == 40 * 60


def test_a_subscription_checkout_still_grants_the_plan(client):
    """The branch above must not swallow ordinary subscriptions."""
    with app.app_context():
        _clinician(plan="free", status=None)
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1", "client_reference_id": "care",
                            "metadata": {"clinician_id": "care", "plan": "paid"}}},
    }
    with patch("billing.verify_webhook", return_value=event):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert db.session.get(Clinician, "care").plan == "paid"


# ---------------------------------------------------------------------------
# Who may buy
# ---------------------------------------------------------------------------

def test_only_caregivers_can_buy_hours(client):
    """Nobody else is metered, so nobody else has hours to buy."""
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        assert client.post("/billing/topup").status_code == 404


def test_buying_requires_signing_in(client):
    with patch.object(config, "BILLING_ENABLED", True):
        assert client.post("/billing/topup").status_code == 403


def test_a_caregiver_can_start_a_topup_checkout(client):
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("billing.create_topup_checkout_url",
               return_value="https://stripe.test/pay") as mk:
        rv = client.post("/billing/topup")
    assert rv.status_code == 303
    assert rv.headers["Location"] == "https://stripe.test/pay"
    mk.assert_called_once()


# ---------------------------------------------------------------------------
# The plans page
# ---------------------------------------------------------------------------

def test_a_caregiver_sees_their_balance_and_a_buy_button(client):
    with app.app_context():
        _clinician(role=roles.CAREGIVER)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        body = client.get("/pricing").get_data(as_text=True)
    assert "Recording time" in body
    assert "Add 40 hours" in body
    assert "not a subscription" in body        # says plainly what they are buying


def test_other_roles_are_not_shown_recording_time(client):
    with app.app_context():
        _clinician(role=roles.PSYCHOTHERAPIST)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        body = client.get("/pricing").get_data(as_text=True)
    assert "Add 40 hours" not in body
