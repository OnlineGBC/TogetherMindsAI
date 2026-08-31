"""
tests/test_partner_payout.py
----------------------------
The partner payout report (step 2 of 2 — the codes and the usage alerts came
first).

The commission exists ONLY in our database. Stripe knows nothing about it, so
this report is the only thing standing between that number and someone being
paid the wrong amount. Two things are therefore checked harder than anything
else here:

  * every money figure comes from what Stripe COLLECTED, never a list price —
    the customer paid a discounted amount;
  * a referral keeps its own copy of the partner and the percentage, so the
    report still answers after the code is edited or deleted.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-payout")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import admin_access
import billing
import config
import roles
from TogetherMindsAI import app
# After the app module: routes_billing imports it, so importing this first leaves
# TogetherMindsAI half-built and its route registration fails.
import routes_billing
from models import (db, init_encryption, Clinician, PromoCode, Referral,
                    ReferralPayment, AuditLog)

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


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


# ---------------------------------------------------------------------------
# Fixtures shaped like the real thing
# ---------------------------------------------------------------------------

def _code(promo_id="promo_x", **kw):
    """A partner's code, as create_promo_code would have left it."""
    args = dict(label="Easton", email="easton@example.com", code="EASTON-AB12",
                discount_pct=40, commission_pct=20, max_uses=None,
                promo_id=promo_id, active=True, created_at=NOW, created_by=ADMIN)
    args.update(kw)
    row = PromoCode(**args)
    db.session.add(row)
    db.session.commit()
    return row


def _clinician(cid="clin-1", customer="cus_1", email="referred@example.com"):
    row = Clinician(id=cid, provider="google", provider_subject=cid, email=email,
                    role=roles.PSYCHOTHERAPIST, created_at=NOW,
                    stripe_customer_id=customer)
    db.session.add(row)
    db.session.commit()
    return row


def _referral(**kw):
    """A referral already recorded, so payment tests can start from the money."""
    args = dict(code="EASTON-AB12", partner_label="Easton",
                partner_email="easton@example.com", commission_pct=20,
                clinician_id="clin-1", stripe_customer_id="cus_1",
                first_payment_at=None, earns_until=None, created_at=NOW)
    args.update(kw)
    row = Referral(**args)
    db.session.add(row)
    db.session.commit()
    return row


def _pay(paid_at=None, **kw):
    """Record a collected payment through the real helper."""
    args = dict(customer_id="cus_1", stripe_ref="in_1", amount_cents=600,
                currency="usd", paid_at=paid_at or NOW)
    args.update(kw)
    return admin_access.record_payment(db, Referral, ReferralPayment, **args)


def _checkout_event(**obj):
    """A SUBSCRIPTION checkout. It carries amount_total like any other session —
    which is exactly why the money must not be read from here: invoice.paid
    reports the same charge, and counting both would pay the partner twice."""
    base = {"id": "cs_sub", "customer": "cus_1", "client_reference_id": "clin-1",
            "payment_intent": None, "amount_total": 600, "currency": "usd",
            "created": int(NOW.timestamp()),
            "metadata": {"clinician_id": "clin-1", "plan": "paid"},
            "discounts": [{"coupon": None, "promotion_code": "promo_x"}]}
    base.update(obj)
    return {"type": "checkout.session.completed", "data": {"object": base}}


def _paid_event(**obj):
    base = {"id": "in_1", "customer": "cus_1", "amount_paid": 600,
            "currency": "usd",
            "status_transitions": {"paid_at": int(NOW.timestamp())}}
    base.update(obj)
    return {"type": "invoice.paid", "data": {"object": base}}


def _as_verified_admin(client, fn):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "BILLING_ENABLED", True),
               patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            db.session.add(Clinician(id="admin", provider="google",
                                     provider_subject="admin", email=ADMIN,
                                     role=roles.PSYCHOTHERAPIST, created_at=NOW))
            db.session.commit()
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
# The share of a collected payment
# ---------------------------------------------------------------------------

def test_the_share_is_taken_from_what_was_collected():
    """$6.00 collected at 20% is $1.20. Not 20% of the $10 list price."""
    assert admin_access.commission_cents(600, 20) == 120


def test_a_half_cent_rounds_up_not_to_the_nearest_even_number():
    """Regression guard on round(): it rounds .5 to the nearest EVEN number, so
    2.5c would become 2 and 3.5c would become 4 — quietly inconsistent. Money is
    expected to round up on the half."""
    assert admin_access.commission_cents(25, 10) == 3       # 2.5c
    assert admin_access.commission_cents(35, 10) == 4       # 3.5c


def test_nobody_to_pay_earns_nothing():
    assert admin_access.commission_cents(1000, 0) == 0
    assert admin_access.commission_cents(0, 40) == 0


# ---------------------------------------------------------------------------
# Reading the code out of a checkout
# ---------------------------------------------------------------------------

def test_the_promotion_code_is_read_when_stripe_sends_a_plain_id():
    obj = {"discounts": [{"coupon": None, "promotion_code": "promo_9"}]}
    assert admin_access.promo_id_from_checkout(obj) == "promo_9"


def test_the_promotion_code_is_read_when_stripe_expands_it_to_an_object():
    """Which shape arrives depends on the account's API version. Guessing wrong
    means a referral is silently never recorded."""
    obj = {"discounts": [{"coupon": None, "promotion_code": {"id": "promo_9"}}]}
    assert admin_access.promo_id_from_checkout(obj) == "promo_9"


def test_a_checkout_with_no_code_reads_as_no_code():
    assert admin_access.promo_id_from_checkout({}) == ""
    assert admin_access.promo_id_from_checkout({"discounts": []}) == ""
    assert admin_access.promo_id_from_checkout(
        {"discounts": [{"coupon": "cpn_1", "promotion_code": None}]}) == ""


# ---------------------------------------------------------------------------
# Recording the referral
# ---------------------------------------------------------------------------

def test_the_referral_copies_the_partner_and_the_share(client):
    """Copied, not looked up later: the code row can be deleted afterwards."""
    with app.app_context():
        _code()
        row = admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_x", clinician_id="clin-1",
            customer_id="cus_1", now=NOW)
        assert row.code == "EASTON-AB12"
        assert row.partner_label == "Easton"
        assert row.partner_email == "easton@example.com"
        assert row.commission_pct == 20
        assert row.stripe_customer_id == "cus_1"
        # No money yet, so no clock has started.
        assert row.first_payment_at is None and row.earns_until is None


def test_a_code_that_is_not_ours_records_nothing(client):
    with app.app_context():
        _code(promo_id="promo_x")
        assert admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_someone_else",
            clinician_id="clin-1", customer_id="cus_1", now=NOW) is None
        assert Referral.query.count() == 0


def test_the_first_code_they_used_is_who_referred_them(client):
    """A second checkout must not move the referral to another partner."""
    with app.app_context():
        _code(promo_id="promo_x", code="EASTON-AB12", label="Easton")
        _code(promo_id="promo_y", code="OTHER-CD34", label="Other")
        admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_x", clinician_id="clin-1",
            customer_id="cus_1", now=NOW)
        assert admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_y", clinician_id="clin-1",
            customer_id="cus_1", now=NOW) is None
        assert Referral.query.count() == 1
        assert Referral.query.first().code == "EASTON-AB12"


def test_a_customer_id_learned_later_is_still_stored(client):
    """Without it, no payment could ever be matched back — Stripe names the
    customer on every payment and the code on none of them."""
    with app.app_context():
        _code()
        admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_x", clinician_id="clin-1",
            customer_id=None, now=NOW)
        admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_x", clinician_id="clin-1",
            customer_id="cus_late", now=NOW)
        assert Referral.query.first().stripe_customer_id == "cus_late"


# ---------------------------------------------------------------------------
# Recording the money
# ---------------------------------------------------------------------------

def test_the_first_collected_payment_starts_the_year(client):
    with app.app_context():
        _referral()
        row = _pay()
        saved = Referral.query.first()
        assert saved.first_payment_at == NOW.replace(tzinfo=None)
        assert saved.earns_until == (NOW + timedelta(days=365)).replace(tzinfo=None)
        assert row.amount_cents == 600 and row.commission_cents == 120
        assert row.in_window is True


def test_a_payment_of_nothing_starts_no_year_and_earns_nothing(client):
    """The 100%-off testing code collects $0. A year of earning must not start on
    a charge that never happened."""
    with app.app_context():
        _referral()
        assert _pay(amount_cents=0) is None
        assert ReferralPayment.query.count() == 0
        assert Referral.query.first().first_payment_at is None


def test_a_renewal_inside_the_year_earns(client):
    with app.app_context():
        _referral()
        _pay(stripe_ref="in_1")
        row = _pay(stripe_ref="in_2", paid_at=NOW + timedelta(days=200))
        assert row.commission_cents == 120 and row.in_window is True


def test_a_payment_after_the_year_earns_nothing(client):
    """The rule the whole per-referral record exists for."""
    with app.app_context():
        _referral()
        _pay(stripe_ref="in_1")
        row = _pay(stripe_ref="in_2", paid_at=NOW + timedelta(days=366))
        assert row.in_window is False
        assert row.commission_cents == 0
        assert row.amount_cents == 600      # still recorded, so the report can say why


def test_the_year_is_measured_from_the_first_payment_not_the_signup(client):
    """They signed up in September and first paid in December; the year runs from
    December."""
    first_paid = NOW + timedelta(days=90)
    with app.app_context():
        _referral()
        _pay(stripe_ref="in_1", paid_at=first_paid)
        row = _pay(stripe_ref="in_2", paid_at=first_paid + timedelta(days=300))
        assert row.in_window is True        # 390 days after signing up
        assert row.commission_cents == 120


def test_the_same_payment_is_never_counted_twice(client):
    """Stripe delivers a webhook more than once. Paying a partner twice for one
    payment is the exact mistake this guard exists to stop."""
    with app.app_context():
        _referral()
        assert _pay(stripe_ref="in_1") is not None
        assert _pay(stripe_ref="in_1") is None
        assert ReferralPayment.query.count() == 1


def test_a_payment_from_someone_nobody_referred_is_ignored(client):
    with app.app_context():
        _referral(stripe_customer_id="cus_1")
        assert _pay(customer_id="cus_stranger", stripe_ref="in_9") is None
        assert ReferralPayment.query.count() == 0


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _report(**kw):
    return admin_access.payout_report(db, Referral, ReferralPayment, Clinician, **kw)


def test_the_report_shows_who_signed_up_what_came_in_and_what_is_owed(client):
    with app.app_context():
        _clinician()
        _referral()
        _pay()
        partners, totals = _report()
        assert len(partners) == 1
        p = partners[0]
        assert p["code"] == "EASTON-AB12" and p["label"] == "Easton"
        assert p["email"] == "easton@example.com"
        assert p["collected_cents"] == 600 and p["owed_cents"] == 120
        assert p["referrals"][0]["who"] == "referred@example.com"
        assert totals == {"collected_cents": 600, "owed_cents": 120, "referrals": 1}


def test_the_report_never_pays_a_share_of_list_price(client):
    """40% off a $10 plan collects $6.00. A 20% partner earns $1.20, not $2.00.
    This is the mistake the whole report exists to prevent."""
    with app.app_context():
        _clinician()
        _referral(commission_pct=20)
        _pay(amount_cents=600)             # what Stripe actually collected
        partners, _ = _report()
        assert partners[0]["owed_cents"] == 120


def test_the_report_survives_the_code_being_deleted(client):
    """delete_promo_code removes the code row outright. The partner is still owed
    what they earned."""
    with app.app_context():
        _clinician()
        code = _code()
        admin_access.referral_from_checkout(
            db, Referral, PromoCode, promo_id="promo_x", clinician_id="clin-1",
            customer_id="cus_1", now=NOW)
        _pay()
        with patch.object(billing, "deactivate_promotion_code"):
            admin_access.delete_promo_code(db, PromoCode, code.id)
        assert PromoCode.query.count() == 0
        partners, totals = _report()
        assert partners[0]["label"] == "Easton"
        assert partners[0]["email"] == "easton@example.com"
        assert totals["owed_cents"] == 120


def test_the_report_uses_the_share_that_applied_when_they_signed_up(client):
    """Editing a code afterwards must not rewrite money already earned."""
    with app.app_context():
        _clinician()
        _referral(commission_pct=20)
        _pay()
        Referral.query.first().commission_pct = 50   # as if the code were edited
        db.session.commit()
        partners, _ = _report()
        assert partners[0]["owed_cents"] == 120      # the stored figure, untouched


def test_the_date_range_limits_the_money_but_not_the_sign_ups(client):
    """A payout run asks "what came in this month". The person still has to be
    listed, or a referral that paid nothing this month would simply vanish."""
    with app.app_context():
        _clinician()
        _referral()
        _pay(stripe_ref="in_1", paid_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
        _pay(stripe_ref="in_2", paid_at=datetime(2026, 10, 15, tzinfo=timezone.utc))
        partners, totals = _report(date_from="2026-10-01", date_to="2026-10-31")
        assert totals["collected_cents"] == 600 and totals["owed_cents"] == 120
        assert len(partners[0]["referrals"]) == 1
        # And with no range, both payments count.
        _, all_time = _report()
        assert all_time["owed_cents"] == 240


def test_a_payment_on_the_last_day_of_the_range_is_included(client):
    """An end date means the whole of that day. Cutting it off at midnight would
    drop the last day's takings from every payout run."""
    with app.app_context():
        _clinician()
        _referral()
        _pay(paid_at=datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc))
        _, totals = _report(date_from="2026-10-01", date_to="2026-10-31")
        assert totals["owed_cents"] == 120


def test_a_referral_that_has_not_paid_yet_is_listed_with_nothing_owed(client):
    with app.app_context():
        _clinician()
        _referral()
        partners, totals = _report()
        assert partners[0]["referrals"][0]["first_payment_at"] is None
        assert totals["owed_cents"] == 0


def test_the_report_says_how_many_payments_fell_past_the_year(client):
    """Otherwise "$0 owed" on a paying customer looks like a bug."""
    with app.app_context():
        _clinician()
        _referral()
        _pay(stripe_ref="in_1")
        _pay(stripe_ref="in_2", paid_at=NOW + timedelta(days=400))
        partners, _ = _report()
        assert partners[0]["referrals"][0]["expired"] == 1
        assert partners[0]["collected_cents"] == 1200   # both came in
        assert partners[0]["owed_cents"] == 120         # only one earned


def test_two_partners_are_reported_separately(client):
    with app.app_context():
        _clinician("clin-1", "cus_1", "a@example.com")
        _clinician("clin-2", "cus_2", "b@example.com")
        _referral(clinician_id="clin-1", stripe_customer_id="cus_1")
        _referral(clinician_id="clin-2", stripe_customer_id="cus_2",
                  code="OTHER-CD34", partner_label="Other",
                  partner_email="other@example.com", commission_pct=30)
        _pay(customer_id="cus_1", stripe_ref="in_1", amount_cents=600)
        _pay(customer_id="cus_2", stripe_ref="in_2", amount_cents=600)
        partners, totals = _report()
        assert {p["code"] for p in partners} == {"EASTON-AB12", "OTHER-CD34"}
        assert totals["owed_cents"] == 120 + 180


def test_one_unreadable_row_does_not_take_the_whole_report_down(client):
    """Referral.partner_email is Fernet-encrypted and decrypts as the row LOADS,
    so a row written under an older key makes a bulk query raise — and the payout
    page would 500 instead of skipping one unreadable name."""
    with app.app_context():
        _clinician()
        _referral()
        _pay()
        # A row whose encrypted column will never decrypt, inserted behind the
        # model so the cipher is bypassed exactly as an old key would look.
        db.session.execute(text(
            "INSERT INTO referrals (code, partner_label, partner_email, "
            "commission_pct, clinician_id, stripe_customer_id, created_at) "
            "VALUES ('OLD-KEY01', 'Older', 'not-a-fernet-token', 25, "
            "'clin-old', 'cus_old', '2026-09-01 12:00:00')"))
        db.session.commit()
        partners, totals = _report()
        assert totals["owed_cents"] == 120          # the good row still reports
        assert "OLD-KEY01" not in {p["code"] for p in partners}


def test_the_report_is_not_capped(client):
    """Every other list on this console shows the most recent N. A payout report
    that quietly stopped short would underpay someone."""
    with app.app_context():
        wanted = admin_access.ACCOUNT_LIST_LIMIT + 10
        for i in range(wanted):
            _referral(clinician_id=f"clin-{i}", stripe_customer_id=f"cus_{i}")
        partners, totals = _report()
        assert totals["referrals"] == wanted


def test_money_reads_as_dollars():
    assert admin_access.money(120) == "$1.20"
    assert admin_access.money(0) == "$0.00"
    assert admin_access.money(123456) == "$1,234.56"


# ---------------------------------------------------------------------------
# The webhook — where every figure comes from
# ---------------------------------------------------------------------------

def test_a_checkout_with_a_partner_code_records_the_referral(client):
    with app.app_context():
        _clinician()
        _code()
    with patch("billing.verify_webhook", return_value=_checkout_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        row = Referral.query.first()
        assert row.code == "EASTON-AB12" and row.commission_pct == 20
        assert AuditLog.query.filter_by(event_type="referral_recorded").count() == 1


def test_a_checkout_with_no_code_records_no_referral(client):
    with app.app_context():
        _clinician()
        _code()
    event = _checkout_event(discounts=[])
    with patch("billing.verify_webhook", return_value=event):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert Referral.query.count() == 0


def test_a_referral_that_cannot_be_recorded_still_grants_the_plan(client):
    """The bookkeeping must never cost someone the subscription they just paid
    for."""
    with app.app_context():
        _clinician()
        _code()
    with patch.object(admin_access, "referral_from_checkout",
                      side_effect=RuntimeError("boom")), \
         patch("billing.verify_webhook", return_value=_checkout_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert db.session.get(Clinician, "clin-1").plan == "paid"


def test_invoice_paid_is_handled_and_records_what_stripe_collected(client):
    """Without this event the report would have no money in it at all: checkout
    names the code but not the renewals, and the subscription events carry the
    plan, not the amount."""
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_paid_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        row = ReferralPayment.query.first()
        assert row.amount_cents == 600 and row.commission_cents == 120
        assert row.stripe_ref == "in_1"
        assert AuditLog.query.filter_by(
            event_type="referral_payment_recorded").count() == 1


def test_a_collected_payment_with_no_paid_at_still_lands(client):
    """Stripe sends status_transitions with a null value on some events, and
    .get on None would raise — losing the payment silently."""
    with app.app_context():
        _clinician()
        _referral()
    event = _paid_event(status_transitions=None, created=int(NOW.timestamp()))
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert ReferralPayment.query.count() == 1


# ---------------------------------------------------------------------------
# Webhook ORDER.
#
# Stripe creates checkout.session.completed and invoice.paid in the same instant
# and does not promise which is delivered first. Both orders were seen on the live
# account: of two real sign-ups, one arrived each way. Every test above feeds the
# events in a sensible order, which is exactly why none of them caught this.
# ---------------------------------------------------------------------------

def _paid_event_with_code(**obj):
    """invoice.paid as Stripe really sends it — the invoice names the promotion
    code too, in the expanded `discount` object."""
    base = {"id": "in_first", "customer": "cus_1", "amount_paid": 600,
            "currency": "usd",
            "status_transitions": {"paid_at": int(NOW.timestamp())},
            "subscription": "sub_1", "billing_reason": "subscription_create",
            "total_discount_amounts": [{"amount": 400, "discount": "di_1"}],
            "discounts": ["di_1"],          # bare id, as Stripe sends it
            "discount": {"id": "di_1", "promotion_code": "promo_x",
                         "coupon": {"id": "tmai_discount_40off"}}}
    base.update(obj)
    return {"type": "invoice.paid", "data": {"object": base}}


def test_the_first_payment_still_counts_when_it_overtakes_the_checkout(client):
    """The live bug. invoice.paid arrived a second BEFORE checkout on one of two
    real sign-ups. There was no referral yet, so the money was dropped — and since
    the webhook answers 200 either way, Stripe never retried it."""
    with app.app_context():
        _clinician()                        # customer id is stored before checkout
        _code()
    with patch("billing.verify_webhook", return_value=_paid_event_with_code()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        ref = Referral.query.first()
        assert ref is not None and ref.code == "EASTON-AB12"
        assert ref.commission_pct == 20
        pay = ReferralPayment.query.first()
        assert pay is not None and pay.amount_cents == 600
        assert pay.commission_cents == 120


def test_the_checkout_arriving_afterwards_does_not_pay_twice(client):
    """Both events name the same code. Whichever lands first does the work."""
    with app.app_context():
        _clinician()
        _code()
    with patch("billing.verify_webhook", return_value=_paid_event_with_code()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with patch("billing.verify_webhook", return_value=_checkout_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert Referral.query.count() == 1
        assert ReferralPayment.query.count() == 1
        _, totals = _report()
        assert totals["owed_cents"] == 120


def test_the_usual_order_still_works(client):
    """Checkout first, then the invoice — the order that was already fine."""
    with app.app_context():
        _clinician()
        _code()
    with patch("billing.verify_webhook", return_value=_checkout_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with patch("billing.verify_webhook", return_value=_paid_event_with_code()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert Referral.query.count() == 1
        assert ReferralPayment.query.count() == 1


def test_a_renewal_for_someone_with_no_code_records_nothing(client):
    """Most customers are nobody's referral. Their renewals must stay out of it.

    A partner code DOES exist here, and a real clinician is paying full price.
    Anything looser than "this invoice names this code" would hand that partner a
    share of a sale they had nothing to do with.
    """
    with app.app_context():
        _clinician()
        _code()
    event = _paid_event_with_code(discount=None, discounts=[],
                                 total_discount_amounts=[])
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert Referral.query.count() == 0
        assert ReferralPayment.query.count() == 0


def test_the_invoice_reader_handles_both_shapes():
    """`discount` is the expanded object Stripe sends on this API version;
    `discounts` may carry objects instead of bare ids on another."""
    assert admin_access.promo_id_from_invoice(
        {"discount": {"promotion_code": "promo_9"}}) == "promo_9"
    assert admin_access.promo_id_from_invoice(
        {"discount": {"promotion_code": {"id": "promo_9"}}}) == "promo_9"
    assert admin_access.promo_id_from_invoice(
        {"discounts": [{"promotion_code": "promo_9"}]}) == "promo_9"
    # Bare discount ids carry no code — this is the deprecated-field trap.
    assert admin_access.promo_id_from_invoice({"discounts": ["di_1"]}) == ""
    assert admin_access.promo_id_from_invoice({}) == ""


def test_a_discount_with_no_readable_code_is_noticed():
    """The failure that looks like nothing at all: money was discounted, so a
    partner may be owed, but no code can be read. Silence here is how the report
    goes quiet without anything appearing broken."""
    assert admin_access.discount_unreadable(
        {"total_discount_amounts": [{"amount": 400, "discount": "di_1"}],
         "discounts": ["di_1"]}) is True
    # Readable, so nothing to say.
    assert admin_access.discount_unreadable(
        {"discount": {"promotion_code": "promo_9"}}) is False
    # No discount at all — an ordinary full-price invoice.
    assert admin_access.discount_unreadable({}) is False


def test_an_unreadable_discount_is_written_to_the_log(client):
    """Warning, not info: info never reaches Cloud Run's logs, so it could not be
    used to diagnose this in production."""
    with app.app_context():
        _clinician()
    event = _paid_event_with_code(discount=None)        # ids only, no code
    with patch("billing.verify_webhook", return_value=event), \
         patch.object(app.logger, "warning") as warned:
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    said = " ".join(str(c) for c in warned.call_args_list)
    assert "promotion code" in said


def test_an_invoice_for_an_unknown_customer_attributes_nothing(client):
    """No clinician holds that Stripe customer id, so there is nobody to credit.

    A clinician DOES exist here, under a different customer id. Matching on
    anything looser would credit the referral to the wrong person entirely.
    """
    with app.app_context():
        _code()
        _clinician(cid="clin-9", customer="cus_someone_else",
                   email="other@example.com")
    event = _paid_event_with_code(customer="cus_nobody", id="in_nobody")
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert Referral.query.count() == 0


def _topup_event(**obj):
    """A one-off hours purchase. mode="payment", so Stripe makes no invoice and
    invoice.paid never fires — this session is the only place the money appears."""
    base = {"id": "cs_1", "customer": "cus_1", "client_reference_id": "clin-1",
            "payment_intent": "pi_1", "amount_total": 999, "currency": "usd",
            "created": int(NOW.timestamp()),
            "metadata": {"clinician_id": "clin-1", "kind": billing.TOPUP_KIND}}
    base.update(obj)
    return {"type": "checkout.session.completed", "data": {"object": base}}


def test_a_topup_counts_as_money_collected_from_the_referral(client):
    """A top-up has no invoice, so without this the partner would earn nothing on
    a purchase their referral really paid for."""
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_topup_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        row = ReferralPayment.query.first()
        assert row.amount_cents == 999          # amount_total, after the discount
        assert row.commission_cents == 200      # 20% of $9.99, rounded half up
        assert row.stripe_ref == "pi_1"


def test_a_topup_shows_up_in_what_is_owed(client):
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_topup_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        _, totals = _report()
        assert totals["collected_cents"] == 999 and totals["owed_cents"] == 200


def test_a_subscription_checkout_records_no_payment(client):
    """The money for a subscription comes from invoice.paid alone. Counting the
    checkout session as well would pay every partner twice on the first month."""
    with app.app_context():
        _clinician()
        _code()
    with patch("billing.verify_webhook", return_value=_checkout_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert Referral.query.count() == 1      # attribution still recorded
        assert ReferralPayment.query.count() == 0


def test_the_same_topup_delivered_twice_pays_once(client):
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_topup_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert ReferralPayment.query.count() == 1


def test_a_topup_that_cannot_be_recorded_still_grants_the_hours(client):
    """They have already paid for the time. Bookkeeping must not cost them it."""
    from models import HoursGrant
    with app.app_context():
        _clinician()
        _referral()
    with patch.object(admin_access, "record_payment",
                      side_effect=RuntimeError("boom")), \
         patch("billing.verify_webhook", return_value=_topup_event()), \
         patch.object(app.logger, "error") as generic_error:
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert HoursGrant.query.filter_by(kind="topup").count() == 1
        assert ReferralPayment.query.count() == 0
    # Handled where it happened, and named. Letting it reach the webhook's catch-all
    # would log "stripe webhook handling error" and say nothing about payouts.
    generic_error.assert_not_called()


def test_a_topup_by_someone_nobody_referred_is_not_credited_to_a_partner(client):
    """A referral DOES exist here, for a different customer. Matching on anything
    looser than the customer id would hand a stranger's purchase to that partner."""
    with app.app_context():
        _clinician()                       # clin-1 / cus_1, Easton's referral
        _referral()
        _clinician(cid="clin-2", customer="cus_2", email="stranger@example.com")
    event = _topup_event(customer="cus_2", client_reference_id="clin-2",
                         payment_intent="pi_stranger",
                         metadata={"clinician_id": "clin-2",
                                   "kind": billing.TOPUP_KIND})
    with patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200
    with app.app_context():
        assert ReferralPayment.query.count() == 0


def test_the_same_invoice_delivered_twice_pays_once(client):
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_paid_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert ReferralPayment.query.count() == 1


# ---------------------------------------------------------------------------
# What a failed webhook does.
#
# Answering 200 tells Stripe the event was dealt with and it never comes back.
# Right for plan state, which the next event corrects. Wrong for money: one
# database hiccup and the payment is gone with nothing to show it existed.
# ---------------------------------------------------------------------------

def _boom(*a, **kw):
    raise RuntimeError("database hiccup")


def test_a_failed_payment_asks_stripe_to_send_it_again(client):
    with app.app_context():
        _clinician()
        _referral()
    with patch.object(routes_billing, "_record_collected_payment", _boom), \
         patch("billing.verify_webhook", return_value=_paid_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 500


def test_a_failed_checkout_asks_stripe_to_send_it_again(client):
    """It carries the top-up money, the referral and the hours. If it fails,
    someone has paid and received nothing."""
    with app.app_context():
        _clinician()
        _code()
    with patch.object(routes_billing, "_apply_checkout_completed", _boom), \
         patch("billing.verify_webhook", return_value=_checkout_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 500


def test_a_failed_plan_update_is_not_retried(client):
    """A stale plan is corrected by the next subscription event. Retrying it for
    three days would add noise, not safety."""
    with app.app_context():
        _clinician()
    event = {"type": "customer.subscription.updated",
             "data": {"object": {"customer": "cus_1", "status": "active"}}}
    with patch.object(routes_billing, "_apply_subscription_change", _boom), \
         patch("billing.verify_webhook", return_value=event):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200


def test_a_retried_payment_is_not_paid_twice(client):
    """Retrying is only safe because replaying cannot double-pay. Proven here
    rather than assumed: the first delivery fails, the second succeeds."""
    with app.app_context():
        _clinician()
        _referral()
    with patch.object(routes_billing, "_record_collected_payment", _boom), \
         patch("billing.verify_webhook", return_value=_paid_event()):
        first = client.post("/stripe/webhook", data=b"{}",
                            headers={"Stripe-Signature": "ok"})
    assert first.status_code == 500
    # Stripe delivers the same event again, and this time nothing is wrong.
    with patch("billing.verify_webhook", return_value=_paid_event()):
        second = client.post("/stripe/webhook", data=b"{}",
                             headers={"Stripe-Signature": "ok"})
        third = client.post("/stripe/webhook", data=b"{}",
                            headers={"Stripe-Signature": "ok"})
    assert second.status_code == 200 and third.status_code == 200
    with app.app_context():
        assert ReferralPayment.query.count() == 1
        _, totals = _report()
        assert totals["owed_cents"] == 120


def test_a_retried_topup_grants_the_hours_once(client):
    """The same proof for the other money event."""
    from models import HoursGrant
    with app.app_context():
        _clinician()
        _referral()
    with patch.object(routes_billing, "_record_topup_payment", _boom), \
         patch("billing.verify_webhook", return_value=_topup_event()):
        first = client.post("/stripe/webhook", data=b"{}",
                            headers={"Stripe-Signature": "ok"})
    assert first.status_code == 500
    with patch("billing.verify_webhook", return_value=_topup_event()):
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
        client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "ok"})
    with app.app_context():
        assert HoursGrant.query.filter_by(kind="topup").count() == 1
        assert ReferralPayment.query.count() == 1
        assert Referral.query.count() == 1


def test_a_payment_that_worked_still_answers_yes(client):
    with app.app_context():
        _clinician()
        _referral()
    with patch("billing.verify_webhook", return_value=_paid_event()):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "ok"})
    assert rv.status_code == 200


def test_a_forged_webhook_is_still_refused_outright(client):
    """Not retried: an unsigned request is not a delivery that went wrong."""
    with patch("billing.verify_webhook", return_value=None):
        rv = client.post("/stripe/webhook", data=b"{}",
                         headers={"Stripe-Signature": "bad"})
    assert rv.status_code == 400


def test_only_the_money_events_are_retried():
    """Records the decision in code, not just in a conversation."""
    assert set(routes_billing.MUST_RETRY) == {
        "invoice.paid", "checkout.session.completed"}
    for state_event in ("customer.subscription.created",
                        "customer.subscription.updated",
                        "customer.subscription.deleted"):
        assert state_event not in routes_billing.MUST_RETRY, state_event


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def test_the_card_shows_the_partner_the_takings_and_the_amount_owed(client):
    def check():
        with app.app_context():
            _clinician()
            _referral()
            _pay()
        with patch.object(billing, "promotion_code_uses", return_value=1):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Partner payouts" in html
        assert "EASTON-AB12" in html
        assert "$6.00" in html            # what Stripe collected
        assert "$1.20" in html            # what is owed
    _as_verified_admin(client, check)


def test_the_card_says_payment_is_by_hand(client):
    """Nothing here sends money, and the screen must not imply that it does."""
    def check():
        with app.app_context():
            _clinician()
            _referral()
            _pay()
        with patch.object(billing, "promotion_code_uses", return_value=1):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "by hand" in html
    _as_verified_admin(client, check)


def test_an_empty_report_says_why_rather_than_reading_as_nobody_earned(client):
    """Counting starts from the day this went live. A blank table with no
    explanation would be read as "no partner is owed anything"."""
    def check():
        with patch.object(billing, "promotion_code_uses", return_value=0):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Tracking starts from the day this was switched on" in html
    _as_verified_admin(client, check)


def test_the_card_filters_by_date_from_the_query_string(client):
    """So a payout run can be bookmarked and handed to the next admin."""
    def check():
        with app.app_context():
            _clinician()
            _referral()
            _pay(paid_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
        with patch.object(billing, "promotion_code_uses", return_value=1):
            html = client.get("/accessadmin?pfrom=2026-10-01&pto=2026-10-31"
                              ).get_data(as_text=True)
        assert "$1.20" not in html        # September money, October range
    _as_verified_admin(client, check)


def test_the_payout_dates_do_not_disturb_the_audit_filters(client):
    """Both cards have From/To boxes. Sharing one name would make filtering a
    payout run silently refilter the activity log."""
    def check():
        with patch.object(billing, "promotion_code_uses", return_value=0):
            html = client.get("/accessadmin?pfrom=2026-10-01").get_data(as_text=True)
        # Attributes wrap across lines in the template, so compare on one line.
        flat = " ".join(html.split())
        assert 'name="pfrom" value="2026-10-01"' in flat
        assert 'name="from" value=""' in flat
    _as_verified_admin(client, check)


def test_the_card_is_hidden_when_billing_is_off(client):
    """With no checkout there are no payments, so there is nothing to pay out."""
    def check():
        with patch.object(config, "BILLING_ENABLED", False):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Partner payouts" not in html
    _as_verified_admin(client, check)


def test_the_report_is_not_reachable_without_the_second_factor(client):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "BILLING_ENABLED", True)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            db.session.add(Clinician(id="admin", provider="google",
                                     provider_subject="admin", email=ADMIN,
                                     role=roles.PSYCHOTHERAPIST, created_at=NOW))
            db.session.commit()
        with client.session_transaction() as s:
            s["user_id"] = "admin"          # signed in, second factor NOT passed
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "Partner payouts" not in html
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# The tables
# ---------------------------------------------------------------------------

def test_the_referral_keeps_its_own_copy_and_no_link_to_the_code(client):
    """A foreign key to promo_codes would either block the delete or take the
    payout record with it."""
    import models
    for col in ("code", "partner_label", "partner_email", "commission_pct"):
        assert hasattr(models.Referral, col), col
    assert not models.Referral.__table__.foreign_keys
    assert not models.ReferralPayment.__table__.foreign_keys


def test_one_referral_per_clinician_is_enforced_by_the_database(client):
    with app.app_context():
        _referral(clinician_id="clin-1")
        with pytest.raises(Exception):
            _referral(clinician_id="clin-1", stripe_customer_id="cus_2")
        db.session.rollback()


def test_a_payment_reference_cannot_repeat_in_the_database(client):
    """The last line of defence behind the duplicate check in record_payment."""
    with app.app_context():
        assert ReferralPayment.__table__.c.stripe_ref.unique is True
