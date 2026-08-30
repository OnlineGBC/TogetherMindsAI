"""
tests/test_partners.py
----------------------
Referral partners and their codes (step 1 of 2 — the payout report and the
usage alerts follow).

A partner refers customers and earns a share. Two percentages, enforced very
differently: the DISCOUNT is real and Stripe applies it at checkout, while the
COMMISSION never leaves our database and is paid by hand. The tests keep that
distinction visible, because on screen the two look alike.
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
os.environ.setdefault("SECRET_KEY", "test-secret-partners")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import admin_access
import billing
import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, PromoCode, DiscountCode, AuditLog

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


def _stripe_ok(promo_id="promo_1"):
    """Stripe accepting the code."""
    return patch.object(billing, "create_promo_code",
                        return_value=MagicMock(id=promo_id))


def _add(**kw):
    args = dict(label="Easton", email="easton@example.com",
                discount_pct=10, commission_pct=40, max_uses=None,
                admin_email=ADMIN)
    args.update(kw)
    return admin_access.create_promo_code(db, PromoCode, **args)


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
# The split — the part that decides whether a sale makes or loses money
# ---------------------------------------------------------------------------

def test_a_normal_split_is_accepted():
    assert admin_access.split_error(10, 40) is None


def test_the_two_shares_need_not_add_to_fifty():
    """The admin sets both numbers; nothing is computed from the other."""
    assert admin_access.split_error(5, 10) is None
    assert admin_access.split_error(30, 30) is None


def test_a_split_that_leaves_nothing_is_refused():
    """50% off AND 50% commission collects less than it pays out — the exact
    mistake this guard exists for."""
    assert admin_access.split_error(50, 50) is not None


def test_a_split_that_leaves_less_than_the_card_fee_is_refused():
    """Stripe takes about 4.7% of a $16 plan. Below that a sale costs more to
    make than it brings in."""
    assert admin_access.split_error(60, 38) is not None    # keeps 2%
    assert admin_access.split_error(60, 35) is None        # keeps 5%


def test_a_discount_outside_one_to_a_hundred_is_refused():
    assert admin_access.split_error(0, 10) is not None
    assert admin_access.split_error(101, 0) is not None


def test_junk_percentages_are_refused_rather_than_crashing():
    assert admin_access.split_error("ten", 40) is not None
    assert admin_access.split_error(None, None) is not None


def test_the_row_reports_what_is_kept(client):
    with app.app_context(), _stripe_ok():
        row = _add(discount_pct=10, commission_pct=40)
        assert row.kept_pct == 50


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------

def test_the_code_carries_a_random_ending():
    """Without it, someone holding EASTON10 would simply try EASTON50 and take
    the bigger discount."""
    a = billing.suggest_code("Easton")
    b = billing.suggest_code("Easton")
    assert a.startswith("EASTON-") and b.startswith("EASTON-")
    assert a != b


def test_the_code_only_uses_characters_stripe_allows():
    """Stripe: lower case, upper case, digits and dashes."""
    import re
    for name in ("Easton O'Neill", "Zoë & Co.", "  spaces  ", ""):
        assert re.fullmatch(r"[A-Za-z0-9-]+", billing.suggest_code(name))


def test_the_code_avoids_letters_that_are_misread_aloud():
    """These codes get read out and typed in. O/0 and I/1/L are where that goes
    wrong."""
    for _ in range(20):
        tail = billing.suggest_code("X").split("-")[1]
        assert not set(tail) & set("O0I1L")


def test_partners_sharing_a_discount_share_one_coupon():
    """A coupon is only a discount definition; the CODE is what is unique per
    partner and what attributes the referral."""
    assert billing.discount_coupon_id(10) == billing.discount_coupon_id(10)
    assert billing.discount_coupon_id(10) != billing.discount_coupon_id(20)


# ---------------------------------------------------------------------------
# Adding and stopping
# ---------------------------------------------------------------------------

def test_adding_a_partner_stores_both_shares_and_the_code(client):
    with app.app_context(), _stripe_ok("promo_x"):
        row = _add()
        assert row.discount_pct == 10 and row.commission_pct == 40
        assert row.promo_id == "promo_x"
        assert row.email == "easton@example.com"
        assert row.active is True


def test_the_max_uses_cap_is_handed_to_stripe(client):
    """Counting redemptions ourselves could be walked past by two people checking
    out at the same moment."""
    with app.app_context():
        with patch.object(billing, "create_promo_code",
                          return_value=MagicMock(id="p1")) as mk:
            _add(max_uses=10)
        assert mk.call_args.args[2] == 10


def test_a_bad_split_never_reaches_stripe(client):
    with app.app_context():
        with patch.object(billing, "create_promo_code") as mk:
            with pytest.raises(ValueError):
                _add(discount_pct=50, commission_pct=50)
        mk.assert_not_called()


def test_nothing_is_stored_when_stripe_refuses(client):
    """A row here with no code there would hand someone a code that does not
    work."""
    with app.app_context():
        with patch.object(billing, "create_promo_code",
                          side_effect=RuntimeError("stripe said no")):
            with pytest.raises(RuntimeError):
                _add()
        assert PromoCode.query.count() == 0


def test_a_partner_needs_a_name(client):
    with app.app_context(), _stripe_ok():
        with pytest.raises(ValueError):
            _add(label="   ")


def test_stopping_a_partner_switches_the_code_off_in_stripe(client):
    with app.app_context(), _stripe_ok("promo_live"):
        row = _add()
        with patch.object(billing, "deactivate_promotion_code") as off:
            assert admin_access.stop_promo_code(db, PromoCode, row.id) is True
        off.assert_called_once_with("promo_live")
        assert db.session.get(PromoCode, row.id).active is False


def test_stopping_twice_is_a_no_op(client):
    with app.app_context(), _stripe_ok():
        row = _add()
        with patch.object(billing, "deactivate_promotion_code"):
            admin_access.stop_promo_code(db, PromoCode, row.id)
            assert admin_access.stop_promo_code(db, PromoCode, row.id) is False


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------

def test_the_card_lists_partners_with_their_code_and_split(client):
    def check():
        with app.app_context(), _stripe_ok():
            _add()
        with patch.object(billing, "promotion_code_uses", return_value=3):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Discount codes" in html
        assert "Easton" in html
        # What each side gets must be readable at a glance, including your own.
        assert "they 10%" in html and "partner 40%" in html and "you 50%" in html
    _as_verified_admin(client, check)


def test_adding_through_the_console_is_written_to_the_audit_log(client):
    def check():
        with _stripe_ok("promo_c"):
            client.post("/accessadmin/code",
                        data={"label": "Easton", "email": "e@example.com",
                              "discount_pct": "10", "commission_pct": "40",
                              "max_uses": "5"},
                        follow_redirects=True)
        with app.app_context():
            assert PromoCode.query.count() == 1
            assert AuditLog.query.filter_by(event_type="promo_code_added").count() == 1
    _as_verified_admin(client, check)


def test_a_bad_split_is_reported_and_nothing_is_saved(client):
    def check():
        html = client.post("/accessadmin/code",
                           data={"label": "Easton", "discount_pct": "50",
                                 "commission_pct": "50"},
                           follow_redirects=True).get_data(as_text=True)
        assert "does not cover the card fee" in html
        with app.app_context():
            assert PromoCode.query.count() == 0
    _as_verified_admin(client, check)


def test_the_card_is_hidden_when_billing_is_off(client):
    def check():
        with patch.object(config, "BILLING_ENABLED", False):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "Discount codes" not in html
    _as_verified_admin(client, check)


def test_an_unverified_admin_cannot_add_a_code(client):
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
        rv = client.post("/accessadmin/code",
                         data={"label": "Sneaky", "discount_pct": "10",
                               "commission_pct": "40"})
        assert rv.status_code == 403
        with app.app_context():
            assert PromoCode.query.count() == 0
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Merging the two cards: the live 100%-off code has to survive
# ---------------------------------------------------------------------------

def test_the_startup_copy_brings_the_legacy_code_across():
    """The two cards became one, so the code that lived in the old single-row
    table has to appear in the merged list. Its Stripe code is untouched — only
    the record of it moves."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "TogetherMindsAI.py"), encoding="utf-8").read()
    block = src.split("Move the one-off discount code into the merged")[1][:1600]
    assert "DiscountCode.query" in block
    assert "PromoCode(" in block
    assert "promo_id=legacy.promo_id" in block      # the SAME Stripe code
    assert "discount_pct=100" in block


def test_the_copy_does_not_run_twice():
    """Startup runs on every boot; a second copy would list the code twice."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "TogetherMindsAI.py"), encoding="utf-8").read()
    block = src.split("Move the one-off discount code into the merged")[1][:1600]
    assert "filter_by(code=legacy.code).first()" in block
    assert "if already is None:" in block


def test_a_failed_copy_never_stops_the_app_booting():
    """Losing the copy costs a row on a screen. Failing to boot costs everything."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "TogetherMindsAI.py"), encoding="utf-8").read()
    block = src.split("Move the one-off discount code into the merged")[1][:1800]
    assert "except Exception" in block
    assert "db.session.rollback()" in block


def test_nothing_writes_to_the_old_table_any_more():
    """It is kept only so the live row can be read and copied."""
    import inspect
    import admin_access as aa
    src = inspect.getsource(aa)
    assert "DiscountCode" not in src


def test_there_is_only_one_way_to_make_a_code():
    """Two cards meant two code paths to change in step. The singleton helpers
    are gone, not left lying around to be called by mistake."""
    import admin_access as aa
    for gone in ("current_discount", "set_discount_code", "turn_off_discount"):
        assert not hasattr(aa, gone), gone


def test_a_plain_code_has_no_partner(client):
    """A commission of 0 means a discount with nobody to pay — the testing code
    is exactly that."""
    with app.app_context(), _stripe_ok():
        row = _add(label="Testing", commission_pct=0, discount_pct=100)
        assert row.has_partner is False
        assert row.kept_pct == 0        # a 100% off code keeps nothing, by design


def test_a_hundred_percent_code_is_still_allowed(client):
    """The testing code gives everything away and earns nothing. The guard is
    about the two shares TOGETHER exceeding what there is, not about generosity."""
    assert admin_access.split_error(100, 0) is None


def test_a_hundred_percent_code_cannot_pay_a_commission():
    """The worst case there is: collect nothing, owe someone a share of it."""
    assert admin_access.split_error(100, 10) is not None
