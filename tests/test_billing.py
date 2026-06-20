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

from datetime import timedelta

import config
import billing
import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, TherapySession

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
    with patch.object(config, "STRIPE_PRICE_PRO", "price_pro"), \
         patch.object(config, "STRIPE_PRICE_PREMIUM", "price_premium"):
        assert billing.plan_for_price("price_premium") == "premium"
        assert billing.plan_for_price("price_pro") == "pro"
        assert billing.plan_for_price("price_other") == "free"


def test_subscription_plan_and_status_from_dict():
    with patch.object(config, "STRIPE_PRICE_PREMIUM", "price_premium"):
        sub = {"status": "active", "items": {"data": [{"price": {"id": "price_premium"}}]}}
        assert billing.subscription_plan_and_status(sub) == ("premium", "active")


# ---- billing page ---------------------------------------------------------

def test_billing_page_requires_login(client):
    rv = client.get("/billing")
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]


def test_billing_page_shows_current_plan(client):
    with app.app_context():
        _clinician(plan="pro", status="active")
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
        _clinician(plan="pro", status="active", customer="cus_5")
    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_5", "status": "active",
                            "items": {"data": [{"price": {"id": "price_premium"}}]}}},
    }
    with patch.object(config, "STRIPE_PRICE_PREMIUM", "price_premium"), \
         patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert db.session.get(Clinician, "clin-1").plan == "premium"


# ---- migration DDL safety -------------------------------------------------

def test_new_timestamp_migrations_are_postgres_safe():
    """Regression: DATETIME is not a valid Postgres type, so an
    'ALTER TABLE ... ADD COLUMN <ts> DATETIME' fails on Cloud SQL and the column
    is silently never created (which 500'd every Clinician query / OAuth login).
    The billing/recording timestamp columns must use TIMESTAMP."""
    import os
    src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "TogetherMindsAI.py")
    src = open(src_path, encoding="utf-8").read()
    assert "current_period_end TIMESTAMP" in src
    assert "reminder_sent_at TIMESTAMP" in src
    assert "current_period_end DATETIME" not in src
    assert "reminder_sent_at DATETIME" not in src


# ---- entitlements ---------------------------------------------------------

def test_billing_off_grants_full_access(client):
    with app.app_context():
        _clinician(plan="free", status=None)
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", False):
            assert tm._effective_plan(c) == "premium"
            assert tm._has_ai_analysis(c) and tm._has_recording(c)


def test_entitlements_enforced_when_billing_on(client):
    with app.app_context():
        _clinician(plan="pro", status="active")
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(c) == "pro"
            assert tm._has_ai_analysis(c)          # Pro ($10) has AI analysis
            assert not tm._has_recording(c)        # but not recording


def test_canceled_paid_plan_is_free(client):
    with app.app_context():
        _clinician(plan="premium", status="canceled")
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(c) == "free"


def _seed_session(sid="s1", therapist="doc"):
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by=therapist, created_at=now,
        retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()


# ---- AI-analysis gating: session summary ----------------------------------

def test_summary_locked_for_free_clinician(client):
    with app.app_context():
        _clinician("doc", plan="free", status="active")
        _seed_session("s1", "doc")
    _login(client, "doc")
    with patch.object(config, "BILLING_ENABLED", True):
        rv = client.get("/session/s1/summary")
    assert rv.status_code == 402
    assert rv.get_json()["locked"] is True


def test_summary_available_for_pro_clinician(client):
    with app.app_context():
        _clinician("doc", plan="pro", status="active")
        _seed_session("s1", "doc")
    _login(client, "doc")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch.object(tm, "_session_summary_payload", return_value={"clinical": "ok"}):
        rv = client.get("/session/s1/summary")
    assert rv.status_code == 200
    assert rv.get_json()["clinical"] == "ok"


# ---- AI-analysis gating: co-pilot (safety stays free) ---------------------

def test_copilot_gates_ai_but_keeps_safety_for_free(client):
    with app.app_context():
        _clinician("doc", plan="free", status="active")
        _seed_session("s1", "doc")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("copilot.build_risk_cards", return_value=[]) as risk, \
         patch("copilot.build_reference_cards", return_value=[]) as ref, \
         patch("copilot.generate_suggestions", return_value=[]) as sug:
        with app.app_context():
            tm._run_copilot("s1", "solo", trigger_text="I feel hopeless")
    risk.assert_called_once()          # safety alerts always run
    ref.assert_not_called()            # AI advisory gated
    sug.assert_not_called()


def test_copilot_runs_ai_for_pro(client):
    with app.app_context():
        _clinician("doc", plan="pro", status="active")
        _seed_session("s1", "doc")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("copilot.build_risk_cards", return_value=[]), \
         patch("copilot.build_reference_cards", return_value=[]) as ref, \
         patch("copilot.generate_suggestions", return_value=[]) as sug:
        with app.app_context():
            tm._run_copilot("s1", "solo", trigger_text="I feel hopeless")
    ref.assert_called_once()
    sug.assert_called_once()
