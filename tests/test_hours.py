"""
tests/test_hours.py
-------------------
Step 6: recording time for caregiver accounts.

40 hours a month, reset monthly, no carry-over. A $9.99 top-up adds 40 more and
DOES carry over — to the end of the following month.

This is the money logic, so the tests lean on the awkward cases: expiry dates,
double-crediting a webhook, spending order, and recording past zero.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-hours")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import hours
from TogetherMindsAI import app
from models import db, init_encryption, HoursGrant

init_encryption(TEST_KEY)

MARCH = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
LATE_MARCH = datetime(2026, 3, 31, 23, 0, tzinfo=timezone.utc)
APRIL = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
MAY = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def test_a_topup_lasts_to_the_end_of_the_following_month():
    """Bought any time in March, good until the last moment of April."""
    assert hours.topup_expiry(MARCH) == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert hours.topup_expiry(LATE_MARCH) == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_month_rollover_works_across_a_year_end():
    dec = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert hours.next_month_start(dec) == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_month_rollover_works_from_a_short_month():
    """A naive +30 days would skip February entirely."""
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert hours.next_month_start(jan31) == datetime(2026, 2, 1, tzinfo=timezone.utc)
    feb = datetime(2026, 2, 15, tzinfo=timezone.utc)
    assert hours.next_month_start(feb) == datetime(2026, 3, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The monthly allowance
# ---------------------------------------------------------------------------

def test_monthly_grant_is_created_once_per_month(ctx):
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)
    assert HoursGrant.query.filter_by(clinician_id="c1", kind="monthly").count() == 1
    assert hours.remaining_minutes(HoursGrant, "c1", MARCH) == 40 * 60


def test_monthly_hours_do_not_carry_over(ctx):
    """Unused March hours are gone in April — that was the agreed rule."""
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)
    hours.consume(db, HoursGrant, "c1", 10 * 60, MARCH)      # used 10 of 40
    assert hours.remaining_minutes(HoursGrant, "c1", APRIL) == 0
    hours.ensure_monthly_grant(db, HoursGrant, "c1", APRIL)
    assert hours.remaining_minutes(HoursGrant, "c1", APRIL) == 40 * 60


# ---------------------------------------------------------------------------
# Top-ups
# ---------------------------------------------------------------------------

def test_a_topup_carries_into_the_next_month_but_not_beyond(ctx):
    hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_1", now=LATE_MARCH)
    assert hours.remaining_minutes(HoursGrant, "c1", LATE_MARCH) == 40 * 60
    assert hours.remaining_minutes(HoursGrant, "c1", APRIL) == 40 * 60   # carried
    assert hours.remaining_minutes(HoursGrant, "c1", MAY) == 0           # expired


def test_the_same_payment_is_never_credited_twice(ctx):
    """Stripe can deliver the same webhook more than once."""
    assert hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_dup", now=MARCH)
    assert hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_dup", now=MARCH) is None
    assert hours.remaining_minutes(HoursGrant, "c1", MARCH) == 40 * 60


def test_two_topups_stack(ctx):
    """Buying two on the last day of the month is allowed, as agreed."""
    hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_a", now=LATE_MARCH)
    hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_b", now=LATE_MARCH)
    assert hours.remaining_minutes(HoursGrant, "c1", LATE_MARCH) == 80 * 60


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------

def test_hours_are_spent_soonest_to_expire_first(ctx):
    """Otherwise someone loses a block they paid for while a longer-lived one
    sits unused."""
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)          # expires 1 Apr
    hours.grant_topup(db, HoursGrant, "c1", stripe_ref="pi_1", now=MARCH)  # expires 1 May
    hours.consume(db, HoursGrant, "c1", 40 * 60, MARCH)

    monthly = HoursGrant.query.filter_by(clinician_id="c1", kind="monthly").first()
    topup = HoursGrant.query.filter_by(clinician_id="c1", kind="topup").first()
    assert monthly.used_minutes == 40 * 60      # the expiring one went first
    assert topup.used_minutes == 0


def test_recording_past_zero_is_reported_not_refused(ctx):
    """A recording already running is never cut off. It can finish slightly over,
    and the NEXT start is what gets blocked."""
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)
    over = hours.consume(db, HoursGrant, "c1", 41 * 60, MARCH)
    assert over == 60                                    # an hour unpaid for
    assert hours.remaining_minutes(HoursGrant, "c1", MARCH) == 0


def test_spending_never_goes_negative(ctx):
    hours.ensure_monthly_grant(db, HoursGrant, "c1", MARCH)
    hours.consume(db, HoursGrant, "c1", 999 * 60, MARCH)
    assert hours.remaining_minutes(HoursGrant, "c1", MARCH) == 0


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def test_each_warning_fires_once():
    assert hours.crossed_warning(6 * 60, 4 * 60) == 5 * 60
    assert hours.crossed_warning(4 * 60, 3 * 60) is None      # already warned
    assert hours.crossed_warning(90, 30) == 60
    assert hours.crossed_warning(30, 5) == 10


def test_a_long_recording_still_reports_the_biggest_threshold_it_passed():
    """A single 6-hour session that jumps from 7 hours left to 1 must not skip
    the warning entirely."""
    assert hours.crossed_warning(7 * 60, 60) == 5 * 60


def test_describe_reads_in_plain_words():
    assert hours.describe(0) == "no recording time left"
    assert hours.describe(45) == "45 minutes left"
    assert hours.describe(60) == "1 hour left"
    assert hours.describe(300) == "5 hours left"
    assert hours.describe(310) == "5h 10m left"
