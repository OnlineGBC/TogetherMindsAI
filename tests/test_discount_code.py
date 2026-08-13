"""
tests/test_discount_code.py
---------------------------
The one discount code the admin console manages.

Stripe will not rename a promotion code — its update endpoint accepts only
`active`, `metadata` and `restrictions`. So changing the code means creating a
new promotion code and switching the old one off, which is what these pin down.
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
os.environ.setdefault("SECRET_KEY", "test-secret-discount")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import admin_access
import billing
import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, DiscountCode, AuditLog

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


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
                                     role=roles.PSYCHOTHERAPIST,
                                     created_at=datetime.now(timezone.utc)))
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
# The code itself
# ---------------------------------------------------------------------------

def test_it_starts_at_the_agreed_code(client):
    with app.app_context():
        row = admin_access.current_discount(db, DiscountCode)
        assert row.code == "100-0ffTh1sBuy"
        assert row.code == billing.DEFAULT_DISCOUNT_CODE


def test_reading_it_creates_nothing_in_stripe(client):
    """A page view must never make live billing objects."""
    with app.app_context():
        row = admin_access.current_discount(db, DiscountCode)
        assert row.promo_id is None


def test_only_one_row_however_often_it_is_read(client):
    with app.app_context():
        admin_access.current_discount(db, DiscountCode)
        admin_access.current_discount(db, DiscountCode)
        assert DiscountCode.query.count() == 1


def test_stripes_character_rule_is_enforced(client):
    """Stripe: "Valid characters are lower case letters (a-z), upper case letters
    (A-Z), digits (0-9), and dashes (-)". The code the user first picked,
    100%0ffTh1sBuy!, would have been rejected by Stripe."""
    assert billing.is_valid_code("100-0ffTh1sBuy") is True
    assert billing.is_valid_code("100%0ffTh1sBuy!") is False
    assert billing.is_valid_code("has space") is False
    assert billing.is_valid_code("") is False


def test_saving_creates_the_new_code_and_switches_the_old_one_off(client):
    with app.app_context():
        row = admin_access.current_discount(db, DiscountCode)
        row.promo_id = "promo_old"
        db.session.commit()

        with patch.object(billing, "create_promotion_code",
                          return_value=MagicMock(id="promo_new")) as create, \
             patch.object(billing, "deactivate_promotion_code") as off:
            admin_access.set_discount_code(db, DiscountCode, "NEW-CODE", ADMIN)

        create.assert_called_once_with("NEW-CODE")
        off.assert_called_once_with("promo_old")     # the OLD one, not the new
        saved = admin_access.current_discount(db, DiscountCode)
        assert saved.code == "NEW-CODE"
        assert saved.promo_id == "promo_new"
        assert saved.active is True


def test_a_stripe_failure_changes_nothing(client):
    """A half-done change must be visible, not stored as if it worked."""
    with app.app_context():
        row = admin_access.current_discount(db, DiscountCode)
        row.promo_id = "promo_old"
        db.session.commit()

        with patch.object(billing, "create_promotion_code",
                          side_effect=RuntimeError("stripe is down")), \
             patch.object(billing, "deactivate_promotion_code") as off:
            with pytest.raises(RuntimeError):
                admin_access.set_discount_code(db, DiscountCode, "NEW-CODE", ADMIN)

        off.assert_not_called()                      # the old one still works
        saved = admin_access.current_discount(db, DiscountCode)
        assert saved.code == "100-0ffTh1sBuy"
        assert saved.promo_id == "promo_old"


def test_a_bad_code_is_refused_before_stripe_is_called(client):
    with app.app_context():
        with patch.object(billing, "create_promotion_code") as create:
            with pytest.raises(ValueError):
                admin_access.set_discount_code(db, DiscountCode, "no spaces!", ADMIN)
        create.assert_not_called()


def test_turning_it_off_switches_it_off_in_stripe_too(client):
    with app.app_context():
        row = admin_access.current_discount(db, DiscountCode)
        row.promo_id = "promo_live"
        db.session.commit()
        with patch.object(billing, "deactivate_promotion_code") as off:
            admin_access.turn_off_discount(db, DiscountCode, ADMIN)
        off.assert_called_once_with("promo_live")
        saved = admin_access.current_discount(db, DiscountCode)
        assert saved.active is False and saved.promo_id is None


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def test_the_card_shows_the_code(client):
    def check():
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "Discount code" in html
        assert 'value="100-0ffTh1sBuy"' in html
        assert "Not created yet" in html          # nothing in Stripe until saved
    _as_verified_admin(client, check)


def test_the_card_is_hidden_when_billing_is_off(client):
    def check():
        with patch.object(config, "BILLING_ENABLED", False):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Discount code" not in html
    _as_verified_admin(client, check)


def test_saving_through_the_console_is_written_to_the_audit_log(client):
    def check():
        with patch.object(billing, "create_promotion_code",
                          return_value=MagicMock(id="promo_x")):
            client.post("/accessadmin/discount",
                        data={"code": "TEST-CODE-1"}, follow_redirects=True)
        with app.app_context():
            assert AuditLog.query.filter_by(event_type="discount_code_set").count() == 1
            assert admin_access.current_discount(db, DiscountCode).code == "TEST-CODE-1"
    _as_verified_admin(client, check)


def test_an_unverified_admin_cannot_change_it(client):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "BILLING_ENABLED", True)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            db.session.add(Clinician(id="admin", provider="google",
                                     provider_subject="admin", email=ADMIN,
                                     role=roles.PSYCHOTHERAPIST,
                                     created_at=datetime.now(timezone.utc)))
            db.session.commit()
        with client.session_transaction() as s:
            s["user_id"] = "admin"          # signed in, second factor NOT passed
        rv = client.post("/accessadmin/discount", data={"code": "SNEAKY"})
        assert rv.status_code == 403
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Both checkouts take a code
# ---------------------------------------------------------------------------

def test_both_checkouts_show_the_code_box():
    fake = MagicMock()
    fake.checkout.Session.create.return_value.url = "https://stripe.test/x"
    with patch.object(config, "STRIPE_PRICE_CLINICAL", "price_clinical"), \
         patch.object(config, "STRIPE_PRICE_HOURS_TOPUP", "price_topup"), \
         patch("billing._init", return_value=fake), \
         patch("billing.ensure_customer", return_value="cus_1"):
        billing.create_checkout_url(MagicMock(id="doc", role=roles.PSYCHOTHERAPIST),
                                    roles.PSYCHOTHERAPIST, "s", "c")
        assert fake.checkout.Session.create.call_args.kwargs["allow_promotion_codes"] is True

        billing.create_topup_checkout_url(MagicMock(id="doc"), "s", "c")
        assert fake.checkout.Session.create.call_args.kwargs["allow_promotion_codes"] is True


# ---------------------------------------------------------------------------
# The names we call must exist on the real SDK
# ---------------------------------------------------------------------------

class _SdkSpecMock:
    """A stand-in for the stripe module that allows only attribute names the
    installed SDK really has, and raises AttributeError for the rest.

    MagicMock(spec=stripe) cannot do this: the SDK resolves names lazily through
    __getattr__, so dir(stripe) is empty and every name would look invalid.
    """

    def __init__(self):
        self._seen = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        import stripe
        if not hasattr(stripe, name):
            raise AttributeError(f"stripe has no attribute {name!r}")
        return self._seen.setdefault(name, MagicMock())


def test_the_stripe_calls_use_names_the_sdk_actually_has():
    """Regression: the code called stripe.promotion_codes.*, which does not exist
    — the snake_case accessors live on a StripeClient instance, not the module.
    Every other test mocks our own wrapper, so nothing ever touched the real
    attribute and the AttributeError only showed up in production.
    """
    import stripe
    assert hasattr(stripe, "PromotionCode")
    assert not hasattr(stripe, "promotion_codes")     # the spelling that broke

    fake = _SdkSpecMock()
    with patch.object(billing, "_init", return_value=fake):
        billing.create_promotion_code("SPEC-TEST")     # raised AttributeError before
        billing.deactivate_promotion_code("promo_1")
        billing.promotion_code_uses("promo_1")

    kwargs = fake.PromotionCode.create.call_args.kwargs
    assert kwargs["code"] == "SPEC-TEST"
    assert kwargs["promotion"] == {"type": "coupon", "coupon": billing.COUPON_ID}
    fake.PromotionCode.modify.assert_called_once_with("promo_1", active=False)
    fake.PromotionCode.retrieve.assert_called_once_with("promo_1")


def test_the_create_parameters_match_the_installed_sdk():
    """`promotion=` is the current shape; an older SDK took `coupon=` instead.
    If the pinned version ever moves back, this says so."""
    from stripe.params import _promotion_code_create_params as params
    allowed = params.PromotionCodeCreateParams.__annotations__
    assert "promotion" in allowed
    assert "code" in allowed
