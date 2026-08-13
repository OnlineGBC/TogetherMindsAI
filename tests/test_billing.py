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
from unittest.mock import patch, MagicMock
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


def _clinician(cid="clin-1", plan=None, status=None, customer=None,
               role="psychotherapist"):
    # A role is required now: a clinician without one is redirected to the role
    # picker before any page loads. Real accounts always have one (set at sign-up,
    # or by the one-time backfill), so these fixtures match production.
    db.session.add(Clinician(
        id=cid, provider="google", provider_subject="sub-" + cid,
        created_at=datetime.now(timezone.utc), role=role,
        plan=plan, subscription_status=status, stripe_customer_id=customer))
    db.session.commit()


def _login(client, cid="clin-1"):
    with client.session_transaction() as s:
        s["clinician_id"] = cid
        s["user_id"] = cid


# ---- pure helpers ---------------------------------------------------------

def test_plan_for_price_mapping():
    """One paid plan now — which features it unlocks is decided by the role."""
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch.object(config, "STRIPE_PRICE_CAREGIVER", "price_caregiver"):
        assert billing.plan_for_price("price_clinical") == "paid"
        assert billing.plan_for_price("price_caregiver") == "paid"
        assert billing.plan_for_price("price_other") == "free"
        assert billing.plan_for_price("") == "free"


def test_retired_prices_no_longer_grant_anything():
    """The old Pro/Premium tiers are gone. An old subscription against one of
    those prices must map to free, not keep granting access under a name nothing
    recognises."""
    with patch.object(config, "STRIPE_PRICE_PRO", "price_pro"), \
         patch.object(config, "STRIPE_PRICE_PREMIUM", "price_premium"), \
         patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch.object(config, "STRIPE_PRICE_CAREGIVER", "price_caregiver"):
        assert billing.plan_for_price("price_pro") == "free"
        assert billing.plan_for_price("price_premium") == "free"


def test_verify_webhook_returns_plain_dict():
    """Regression: construct_event returns a Stripe StripeObject that doesn't
    support dict .get() — which 500'd the live webhook. verify_webhook must return
    a plain dict (parsed from the verified payload)."""
    fake_stripe = MagicMock()
    fake_stripe.Webhook.construct_event.return_value = object()   # signature OK
    payload = b'{"type": "checkout.session.completed", "data": {"object": {"customer": "cus_x"}}}'
    with patch.object(config, "STRIPE_WEBHOOK_SECRET", "whsec_x"), \
         patch("billing._init", return_value=fake_stripe):
        out = billing.verify_webhook(payload, "sig")
    assert isinstance(out, dict)
    assert out["type"] == "checkout.session.completed"
    assert out["data"]["object"]["customer"] == "cus_x"


def test_subscription_plan_and_status_from_dict():
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"):
        sub = {"status": "active", "items": {"data": [{"price": {"id": "price_clinical"}}]}}
        assert billing.subscription_plan_and_status(sub) == ("paid", "active")


# ---- billing page ---------------------------------------------------------

def test_billing_page_is_public_without_login(client):
    """Pricing is public: an anonymous visitor gets the page (200), not a redirect.
    The subscribe button sends them to clinician sign-in rather than checkout."""
    rv = client.get("/billing")
    assert rv.status_code == 200
    assert b"Sign in to subscribe" in rv.data


def test_billing_page_shows_current_plan(client):
    with app.app_context():
        _clinician(plan="paid", status="active")
    _login(client)
    rv = client.get("/billing")
    assert rv.status_code == 200
    assert b"Plans" in rv.data


def test_success_banner_confirms_when_plan_active(client):
    with app.app_context():
        _clinician(plan="paid", status="active")
    _login(client)
    body = client.get("/billing?success=1").data.decode()
    assert "You're subscribed" in body
    assert "being activated" not in body      # no stale "activating" message
    assert "Current plan" in body             # current-plan button, not "Your plan"


def test_success_banner_pending_when_still_free(client):
    """Back from checkout but the webhook has not landed yet — keep the waiting
    message rather than claiming they are subscribed. Needs billing ON, since with
    it off every account counts as paid."""
    with app.app_context():
        _clinician(plan="free", status=None)
    _login(client)
    with patch.object(config, "BILLING_ENABLED", True):
        body = client.get("/billing?success=1").data.decode()
    assert "being activated" in body


def test_billing_shows_renewal_date(client):
    with app.app_context():
        _clinician(plan="paid", status="active")
        c = db.session.get(Clinician, "clin-1")
        c.current_period_end = datetime(2026, 7, 15, tzinfo=timezone.utc)
        db.session.commit()
    _login(client)
    body = client.get("/billing").data.decode()
    assert "Renews on" in body and "15 Jul 2026" in body


# ---- checkout -------------------------------------------------------------

def test_checkout_requires_login(client):
    assert client.post("/billing/checkout/paid").status_code == 403


def test_checkout_404_when_billing_disabled(client):
    with app.app_context():
        _clinician()
    _login(client)
    with patch.object(config, "BILLING_ENABLED", False):
        assert client.post("/billing/checkout/paid").status_code == 404


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
        rv = client.post("/billing/checkout/paid")
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
                            "metadata": {"clinician_id": "clin-1", "plan": "paid"}}},
    }
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        clin = db.session.get(Clinician, "clin-1")
        assert clin.plan == "paid"
        assert clin.subscription_status == "active"


def test_webhook_subscription_deleted_downgrades_to_free(client):
    with app.app_context():
        _clinician(plan="paid", status="active", customer="cus_9")
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
        _clinician(plan="free", status=None, customer="cus_5")
    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_5", "status": "active",
                            "items": {"data": [{"price": {"id": "price_clinical"}}]}}},
    }
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert db.session.get(Clinician, "clin-1").plan == "paid"


def test_webhook_on_a_retired_price_downgrades_to_free(client):
    """Someone still on an old Pro/Premium subscription must not keep access when
    Stripe next reports on it — that tier no longer exists."""
    with app.app_context():
        _clinician(plan="paid", status="active", customer="cus_old")
    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_old", "status": "active",
                            "items": {"data": [{"price": {"id": "price_premium"}}]}}},
    }
    with patch.object(config, "STRIPE_PRICE_PREMIUM", "price_premium"), \
         patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch("billing.verify_webhook", return_value=event):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert db.session.get(Clinician, "clin-1").plan == "free"


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
            assert tm._effective_plan(c) == "paid"
            assert tm._has_ai_analysis(c) and tm._has_recording(c)


def test_entitlements_enforced_when_billing_on(client):
    """One paid plan unlocks everything the ROLE offers — for a psychotherapist
    that is the co-pilot, the recap, recording and ICD codes together."""
    with app.app_context():
        _clinician(plan="paid", status="active")
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(c) == "paid"
            assert tm._has_ai_analysis(c)
            assert tm._has_recording(c)
            assert tm._has_icd_codes(c)


def test_free_plan_grants_nothing_paid(client):
    with app.app_context():
        _clinician(plan="free", status=None)
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._has_ai_analysis(c) is False
            assert tm._has_recording(c) is False
            assert tm._has_icd_codes(c) is False


def test_canceled_paid_plan_is_free(client):
    with app.app_context():
        _clinician(plan="paid", status="canceled")
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(c) == "free"


def test_a_retired_plan_value_grants_nothing(client):
    """An account still carrying "premium" from the old tiers must not keep
    access — that plan name no longer means anything."""
    with app.app_context():
        _clinician(plan="premium", status="active")
        c = db.session.get(Clinician, "clin-1")
        with patch.object(config, "BILLING_ENABLED", True):
            assert tm._effective_plan(c) == "free"
            assert tm._has_recording(c) is False


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


def test_summary_available_for_a_paid_clinician(client):
    with app.app_context():
        _clinician("doc", plan="paid", status="active")
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


def test_copilot_runs_ai_for_a_paid_clinician(client):
    with app.app_context():
        _clinician("doc", plan="paid", status="active")
        _seed_session("s1", "doc")
    with patch.object(config, "BILLING_ENABLED", True), \
         patch("copilot.build_risk_cards", return_value=[]), \
         patch("copilot.build_reference_cards", return_value=[]) as ref, \
         patch("copilot.generate_suggestions", return_value=[]) as sug:
        with app.app_context():
            tm._run_copilot("s1", "solo", trigger_text="I feel hopeless")
    ref.assert_called_once()
    sug.assert_called_once()


def test_billing_routes_extracted_to_module(client):
    """The billing routes now live in routes_billing.py but are attached with
    their ORIGINAL endpoint names, so url_for(...) and templates are unchanged."""
    for ep in ("billing_page", "billing_checkout", "billing_portal", "stripe_webhook"):
        assert ep in app.view_functions, ep
        assert app.view_functions[ep].__module__ == "routes_billing", ep


def test_subscription_checkout_accepts_a_promotion_code():
    """Without this flag Stripe shows no code box, so a 100%-off coupon has
    nowhere to be typed and the live payment path cannot be tested for free."""
    from unittest.mock import MagicMock
    import roles
    fake = MagicMock()
    fake.checkout.Session.create.return_value.url = "https://stripe.test/x"
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch("billing._init", return_value=fake), \
         patch("billing.ensure_customer", return_value="cus_1"):
        billing.create_checkout_url(
            MagicMock(id="doc", role=roles.PSYCHOTHERAPIST),
            roles.PSYCHOTHERAPIST, "s", "c")
    kwargs = fake.checkout.Session.create.call_args.kwargs
    assert kwargs["allow_promotion_codes"] is True
    assert kwargs["mode"] == "subscription"
