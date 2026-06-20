"""
tests/test_billing.py
----------------------
Phase 4 Step 4 — Stripe subscription plumbing: billing page, Checkout redirect,
billing portal, and the signed webhook that drives plan/status. Stripe network
calls are mocked; no card data and no real Stripe key are involved.
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
os.environ.setdefault("SECRET_KEY", "test-secret-billing")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from datetime import datetime, timezone

import config
import billing
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def client():
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db._app_engines[app] = {None: eng}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _clinician(cid="clin-1", plan=None, status=None, customer=None):
    db.session.add(Clinician(
        id=cid, provider="google", provider_subject="sub-" + cid,
        created_at=datetime.now(timezone.utc),
        plan=plan, subscription_status=status, stripe_customer_id=customer))
    db.session.commit()


def _login(client, cid="clin-1"):
    with client.session_transaction() as s:
        s["clinician_id"] = cid
        s["user_id"] = cid


# ---- pure helpers ---------------------------------------------------------

def test_plan_for_price_mapping():
    with patch.object(config, "STRIPE_PRICE_PLUS", "price_plus"), \
         patch.object(config, "STRIPE_PRICE_PRO", "price_pro"):
        assert billing.plan_for_price("price_pro") == "pro"
        assert billing.plan_for_price("price_plus") == "plus"
        assert billing.plan_for_price("price_other") == "free"


def test_subscription_plan_and_status_from_dict():
    with patch.object(config, "STRIPE_PRICE_PRO", "price_pro"):
        sub = {"status": "active", "items": {"data": [{"price": {"id": "price_pro"}}]}}
        assert billing.subscription_plan_and_status(sub) == ("pro", "active")


# ---- billing page ---------------------------------------------------------

def test_billing_page_requires_login(client):
    rv = client.get("/billing")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]


def test_billing_page_shows_current_plan(client):
    with app.app_context():
        _clinician(plan="plus", status="active")
    _login(client)
    rv = client.get("/billing")
    assert rv.status_code == 200
    assert b"Plans" in rv.data


# ---- checkout -------------------------------------------------------------

def test_checkout_requires_login(client):
    assert client.post("/billing/checkout/pro").status_code == 403


def test_checkout_404_when_billing_disabled(client):
    with app.app_context():
        _clinician()
    _login(client)
    with patch.object(config, "BILLING_ENABLED", False):
        assert client.post("/billing/checkout/pro").status_code == 404


def test_checkout_404_for_unknown_plan(client):
    with app.app_context():
        _clinician()
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        assert client.post("/billing/checkout/enterprise").status_code == 404


def test_checkout_redirects_to_stripe(client):
    with app.app_context():
        _clinician()
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("billing.create_checkout_url", return_value="https://checkout.stripe.test/abc") as mk:
        rv = client.post("/billing/checkout/pro")
    assert rv.status_code == 303
    assert rv.headers["Location"] == "https://checkout.stripe.test/abc"
    mk.assert_called_once()


# ---- webhook --------------------------------------------------------------

def test_webhook_rejects_bad_signature(client):
    with patch("billing.verify_webhook", return_value=None):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "bad"})
    assert rv.status_code == 400


def test_webhook_checkout_completed_sets_plan(client):
    with app.app_context():
        _clinician(customer="cus_1")
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1",
                            "client_reference_id": "clin-1",
                            "metadata": {"clinician_id": "clin-1", "plan": "pro"}}},
    }
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        clin = db.session.get(Clinician, "clin-1")
        assert clin.plan == "pro"
        assert clin.subscription_status == "active"


def test_webhook_subscription_deleted_downgrades_to_free(client):
    with app.app_context():
        _clinician(plan="pro", status="active", customer="cus_9")
    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": "cus_9"}},
    }
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        clin = db.session.get(Clinician, "clin-1")
        assert clin.plan == "free"
        assert clin.subscription_status == "canceled"


def test_webhook_subscription_updated_sets_plan_from_price(client):
    with app.app_context():
        _clinician(plan="plus", status="active", customer="cus_5")
    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_5", "status": "active",
                            "items": {"data": [{"price": {"id": "price_pro"}}]}}},
    }
    with patch.object(config, "STRIPE_PRICE_PRO", "price_pro"), \
         patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert db.session.get(Clinician, "clin-1").plan == "pro"
