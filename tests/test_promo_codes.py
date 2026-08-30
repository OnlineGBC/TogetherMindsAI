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


def test_any_split_the_admin_chooses_is_accepted():
    """The console does not argue with the numbers — it shows what is left over
    and saves what was asked for. Only what Stripe itself rejects is refused."""
    assert admin_access.split_error(50, 50) is None
    assert admin_access.split_error(60, 38) is None
    assert admin_access.split_error(100, 0) is None


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


def test_a_discount_stripe_would_reject_never_reaches_it(client):
    with app.app_context():
        with patch.object(billing, "create_promo_code") as mk:
            with pytest.raises(ValueError):
                _add(discount_pct=0, commission_pct=10)
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


# ---------------------------------------------------------------------------
# Editing and deleting
# ---------------------------------------------------------------------------

def test_the_three_editable_fields_change(client):
    with app.app_context(), _stripe_ok():
        row = _add()
        admin_access.edit_promo_code(db, PromoCode, row.id, label="Easton Jr",
                                     email="new@example.com", commission_pct="25")
        saved = db.session.get(PromoCode, row.id)
        assert saved.label == "Easton Jr"
        assert saved.email == "new@example.com"
        assert saved.commission_pct == 25


def test_editing_never_touches_what_stripe_fixed(client):
    """Its update endpoint takes only active, metadata and restrictions — the code,
    the discount and the cap cannot be changed after creation at all."""
    with app.app_context(), _stripe_ok():
        row = _add(discount_pct=10, max_uses=10)
        before = (row.code, row.discount_pct, row.max_uses)
        admin_access.edit_promo_code(db, PromoCode, row.id, label="Renamed")
        saved = db.session.get(PromoCode, row.id)
        assert (saved.code, saved.discount_pct, saved.max_uses) == before


def test_unticking_active_switches_the_code_off_in_stripe(client):
    with app.app_context(), _stripe_ok("promo_live"):
        row = _add()
        with patch.object(billing, "deactivate_promotion_code") as off:
            admin_access.edit_promo_code(db, PromoCode, row.id, active=False)
        off.assert_called_once_with("promo_live")
        assert db.session.get(PromoCode, row.id).active is False


def test_reticking_active_switches_it_back_on(client):
    with app.app_context(), _stripe_ok("promo_live"):
        row = _add()
        with patch.object(billing, "deactivate_promotion_code"):
            admin_access.edit_promo_code(db, PromoCode, row.id, active=False)
        with patch.object(billing, "reactivate_promotion_code") as on:
            admin_access.edit_promo_code(db, PromoCode, row.id, active=True)
        on.assert_called_once_with("promo_live")
        assert db.session.get(PromoCode, row.id).active is True


def test_saving_without_changing_active_does_not_call_stripe(client):
    """Editing a label must not send a needless write to a live billing object."""
    with app.app_context(), _stripe_ok():
        row = _add()
        with patch.object(billing, "deactivate_promotion_code") as off,              patch.object(billing, "reactivate_promotion_code") as on:
            admin_access.edit_promo_code(db, PromoCode, row.id, label="X", active=True)
        off.assert_not_called()
        on.assert_not_called()


def test_an_empty_label_is_refused(client):
    with app.app_context(), _stripe_ok():
        row = _add()
        with pytest.raises(ValueError):
            admin_access.edit_promo_code(db, PromoCode, row.id, label="  ")


def test_editing_an_unknown_code_returns_none(client):
    with app.app_context():
        assert admin_access.edit_promo_code(db, PromoCode, 9999, label="X") is None


def test_deleting_removes_the_row_and_switches_it_off_in_stripe(client):
    """Stripe has no delete for a promotion code, so it is switched off there and
    the row goes here — from the console it is gone and the code stops working."""
    with app.app_context(), _stripe_ok("promo_live"):
        row = _add()
        with patch.object(billing, "deactivate_promotion_code") as off:
            assert admin_access.delete_promo_code(db, PromoCode, row.id) is True
        off.assert_called_once_with("promo_live")
        assert PromoCode.query.count() == 0


def test_deleting_twice_is_a_no_op(client):
    with app.app_context(), _stripe_ok():
        row = _add()
        with patch.object(billing, "deactivate_promotion_code"):
            admin_access.delete_promo_code(db, PromoCode, row.id)
            assert admin_access.delete_promo_code(db, PromoCode, row.id) is False


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
        # The commission is an input now, so it reads as a value rather than text.
        assert "they 10%" in html and "you 50%" in html
        assert 'name="commission_pct" type="number" min="0" max="100"' in html
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


def test_a_percentage_out_of_range_is_reported_and_nothing_is_saved(client):
    def check():
        html = client.post("/accessadmin/code",
                           data={"label": "Easton", "discount_pct": "0",
                                 "commission_pct": "10"},
                           follow_redirects=True).get_data(as_text=True)
        assert "between 1 and 100" in html
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


def test_a_hundred_percent_code_is_allowed(client):
    """The testing code gives everything away and earns nothing."""
    assert admin_access.split_error(100, 0) is None



def test_the_edit_inputs_point_at_the_form_by_id(client):
    """A <form> may not wrap <td>s — browsers hoist it out of the table and the
    fields then submit nothing. The inputs live in different cells, so they
    reference the form by id instead."""
    def check():
        with app.app_context(), _stripe_ok():
            _add()
        with patch.object(billing, "promotion_code_uses", return_value=0):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert 'name="edit_id"' in html
        assert html.count('form="pc') >= 4          # label, email, commission, active
        # And the form element must not be sitting between cells.
        assert "</td>\n                <form" not in html
    _as_verified_admin(client, check)


def test_editing_through_the_console_saves_and_is_logged(client):
    def check():
        with app.app_context(), _stripe_ok():
            row = _add()
            row_id = row.id
        client.post("/accessadmin/code",
                    data={"edit_id": str(row_id), "label": "Renamed",
                          "email": "e2@example.com", "commission_pct": "15",
                          "active": "1"},
                    follow_redirects=True)
        with app.app_context():
            saved = db.session.get(PromoCode, row_id)
            assert saved.label == "Renamed" and saved.commission_pct == 15
            assert AuditLog.query.filter_by(event_type="promo_code_edited").count() == 1
    _as_verified_admin(client, check)


def test_an_unticked_box_switches_the_code_off(client):
    """A checkbox that is not ticked sends nothing at all, which is how the form
    says "off" — reading it as "unchanged" would make the box impossible to clear."""
    def check():
        with app.app_context(), _stripe_ok():
            row = _add()
            row_id = row.id
        with patch.object(billing, "deactivate_promotion_code"):
            client.post("/accessadmin/code",
                        data={"edit_id": str(row_id), "label": "Easton",
                              "commission_pct": "40"},      # no "active" key
                        follow_redirects=True)
        with app.app_context():
            assert db.session.get(PromoCode, row_id).active is False
    _as_verified_admin(client, check)


def test_deleting_through_the_console_removes_it_and_is_logged(client):
    def check():
        with app.app_context(), _stripe_ok():
            row = _add()
            row_id = row.id
        with patch.object(billing, "deactivate_promotion_code"):
            client.post("/accessadmin/code",
                        data={"delete_id": str(row_id)}, follow_redirects=True)
        with app.app_context():
            assert PromoCode.query.count() == 0
            assert AuditLog.query.filter_by(event_type="promo_code_deleted").count() == 1
    _as_verified_admin(client, check)


def test_delete_asks_first(client):
    """It cannot be undone, and it takes the payout record with it."""
    def check():
        with app.app_context(), _stripe_ok():
            _add()
        with patch.object(billing, "promotion_code_uses", return_value=0):
            html = client.get("/accessadmin").get_data(as_text=True)
        assert "return confirm(" in html
    _as_verified_admin(client, check)


# ---------------------------------------------------------------------------
# Usage alerts
#
# The rules are pure functions taking a count — no Stripe, no SMTP, no clock —
# so each one is checked directly rather than through a mock of the world.
# ---------------------------------------------------------------------------

def _row(**kw):
    """A stand-in code row. Not saved: alerts_for only reads attributes."""
    from types import SimpleNamespace
    base = dict(code="X-1", label="Easton", email="e@example.com", max_uses=10,
                last_seen_uses=0, alerted_nearly=False, alerted_spent=False,
                last_burst_alert=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_nothing_is_said_below_the_threshold():
    # burst_per_sweep high on purpose: this is about the 80% rule, and a jump from
    # 0 to 7 in one sweep is a burst in its own right.
    assert admin_access.alerts_for(_row(), 7, nearly_pct=80, burst_per_sweep=99) == []


def test_eighty_percent_used_is_flagged():
    out = admin_access.alerts_for(_row(), 8, nearly_pct=80, burst_per_sweep=99)
    assert out == [admin_access.ALERT_NEARLY]


def test_fully_used_is_flagged():
    out = admin_access.alerts_for(_row(), 10, nearly_pct=80, burst_per_sweep=99)
    assert out == [admin_access.ALERT_SPENT]


def test_a_code_that_jumps_past_the_cap_says_it_has_stopped_not_that_it_is_close():
    """Both rules match at once. Saying "nearly there" about a code that has
    already stopped working would be wrong."""
    out = admin_access.alerts_for(_row(), 12, nearly_pct=80, burst_per_sweep=99)
    assert out == [admin_access.ALERT_SPENT]


def test_each_threshold_is_only_said_once():
    """Without this the same email would arrive every hour for the rest of the
    code's life."""
    assert admin_access.alerts_for(_row(alerted_nearly=True, last_seen_uses=8), 8,
                                   nearly_pct=80, burst_per_sweep=99) == []
    assert admin_access.alerts_for(_row(alerted_spent=True, last_seen_uses=10), 10,
                                   nearly_pct=80, burst_per_sweep=99) == []


def test_a_code_with_no_cap_gets_no_threshold_alerts():
    """80% of nothing is nothing. Only the burst rule applies."""
    out = admin_access.alerts_for(_row(max_uses=None, last_seen_uses=499), 500,
                                  nearly_pct=80, burst_per_sweep=99)
    assert out == []


def test_a_burst_is_flagged():
    """Five sign-ups in an hour on one code is what being passed around looks
    like."""
    out = admin_access.alerts_for(_row(max_uses=None, last_seen_uses=0), 5,
                                  nearly_pct=80, burst_per_sweep=5)
    assert out == [admin_access.ALERT_BURST]


def test_a_burst_is_measured_since_the_last_sweep_not_from_zero():
    out = admin_access.alerts_for(_row(max_uses=None, last_seen_uses=100), 102,
                                  nearly_pct=80, burst_per_sweep=5)
    assert out == []


def test_a_recent_burst_alert_silences_the_next_one():
    out = admin_access.alerts_for(_row(max_uses=None), 9, nearly_pct=80,
                                  burst_per_sweep=5, burst_recent=True)
    assert out == []


def test_no_answer_from_stripe_says_nothing():
    """Silence beats a wrong alarm when the count is unknown."""
    assert admin_access.alerts_for(_row(), None, nearly_pct=80, burst_per_sweep=5) == []


def test_the_partner_and_every_admin_are_told():
    to = admin_access.alert_recipients(_row(email="e@example.com"),
                                       ["a@x.com", "b@x.com"])
    assert to == ["e@example.com", "a@x.com", "b@x.com"]


def test_a_code_with_no_partner_goes_to_admins_only():
    assert admin_access.alert_recipients(_row(email=None), ["a@x.com"]) == ["a@x.com"]


def test_a_partner_who_is_also_an_admin_is_not_told_twice():
    to = admin_access.alert_recipients(_row(email="a@x.com"), ["a@x.com"])
    assert to == ["a@x.com"]


def test_the_messages_say_what_happened_in_plain_words():
    row = _row()
    for kind in (admin_access.ALERT_NEARLY, admin_access.ALERT_SPENT,
                 admin_access.ALERT_BURST):
        subject, body = admin_access.alert_message(kind, row, 8)
        assert row.code in subject and row.code in body
        assert row.label in body


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def _sweep(sent_box, uses, **kw):
    def send(to, subject, body):
        sent_box.append((to, subject))
    args = dict(send=send, now=datetime.now(timezone.utc), nearly_pct=80,
                burst_per_sweep=5, admin_emails=["admin@x.com"],
                uses_of=lambda row: uses)
    args.update(kw)
    return admin_access.sweep_promo_alerts(db, PromoCode, **args)


def test_the_sweep_sends_and_records_that_it_sent(client):
    with app.app_context(), _stripe_ok():
        row = _add(max_uses=10)
        box = []
        # burst_per_sweep high: a brand-new code jumping 0 to 10 is also a burst,
        # and this test is about the cap being reached.
        assert _sweep(box, 10, burst_per_sweep=99) == [(row.code, admin_access.ALERT_SPENT)]
        assert len(box) == 1
        assert db.session.get(PromoCode, row.id).alerted_spent is True


def test_the_sweep_does_not_send_the_same_alert_twice(client):
    with app.app_context(), _stripe_ok():
        _add(max_uses=10)
        box = []
        _sweep(box, 10, burst_per_sweep=99)
        _sweep(box, 10, burst_per_sweep=99)
        assert len(box) == 1


def test_a_failed_email_is_not_marked_as_sent(client):
    """A warning nobody received is worse than a duplicate — the next sweep must
    try again."""
    with app.app_context(), _stripe_ok():
        row = _add(max_uses=10)

        def boom(to, subject, body):
            raise RuntimeError("smtp down")

        admin_access.sweep_promo_alerts(
            db, PromoCode, send=boom, now=datetime.now(timezone.utc),
            nearly_pct=80, burst_per_sweep=5, admin_emails=["a@x.com"],
            uses_of=lambda r: 10)
        assert db.session.get(PromoCode, row.id).alerted_spent is False


def test_the_count_moves_even_when_nothing_is_sent(client):
    """Otherwise a quiet hour would read as a burst on the next sweep."""
    with app.app_context(), _stripe_ok():
        row = _add(max_uses=100)
        _sweep([], 3)
        assert db.session.get(PromoCode, row.id).last_seen_uses == 3


def test_a_switched_off_code_is_skipped(client):
    with app.app_context(), _stripe_ok():
        row = _add(max_uses=10)
        with patch.object(billing, "deactivate_promotion_code"):
            admin_access.edit_promo_code(db, PromoCode, row.id, active=False)
        box = []
        assert _sweep(box, 10) == []
        assert box == []


def test_stripe_being_unreachable_does_not_stop_the_sweep(client):
    with app.app_context(), _stripe_ok():
        _add(max_uses=10)

        def boom(row):
            raise RuntimeError("stripe down")

        assert admin_access.sweep_promo_alerts(
            db, PromoCode, send=lambda *a: None, now=datetime.now(timezone.utc),
            nearly_pct=80, burst_per_sweep=5, admin_emails=["a@x.com"],
            uses_of=boom) == []


def test_the_sweep_is_registered_and_defined_before_it_is_used():
    """The startup block runs at import, so a job registered above its own
    function is an import-time NameError under gunicorn."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "TogetherMindsAI.py"), encoding="utf-8").read()
    def_pos = src.index("def _promo_alert_sweep")
    use_pos = src.index('id="promo_alert_sweep"')
    assert def_pos < use_pos


def test_a_spent_code_never_falls_back_to_saying_nearly():
    """Regression: the two rules were chained on the SENT flags, so a code already
    flagged as fully used sent "nearly used up" on the very next sweep — wrong,
    and a second email."""
    out = admin_access.alerts_for(_row(alerted_spent=True, last_seen_uses=10), 10,
                                  nearly_pct=80, burst_per_sweep=99)
    assert out == []
