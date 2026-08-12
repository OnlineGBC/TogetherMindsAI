"""
hours.py
--------
Recording time for caregiver accounts: what they have, what they have used, and
what happens when it runs out.

The plan includes 40 hours a month. Hours reset each month and do not carry over.
A $9.99 top-up adds 40 more, and a top-up DOES carry over — to the end of the
following month, then it expires.

Modelled as a ledger of grants rather than one running total, because hours
arrive with different expiry dates. Consumption is recorded against the grant it
came out of, so a carried-over block cannot be counted twice.

Flask-free: `db` and the model are passed in, so every rule here is directly
testable without an app.
"""
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

MONTHLY_MINUTES = 40 * 60      # what the plan includes
TOPUP_MINUTES   = 40 * 60      # what $9.99 buys

MONTHLY = "monthly"
TOPUP   = "topup"

# Warn when this much time is left. Ordered largest first.
WARN_AT_MINUTES = (5 * 60, 60, 10)


def _as_utc(dt):
    """DB values come back naive; treat them as the UTC they were stored as."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    """First moment of the following month."""
    first = month_start(now)
    # Day 28 exists in every month, so +4 days always lands in the next month.
    return month_start(first + timedelta(days=32))


def topup_expiry(now: datetime) -> datetime:
    """End of the FOLLOWING month — a block bought any time in March is good
    until the last moment of April."""
    return next_month_start(next_month_start(now))


def ensure_monthly_grant(db, HoursGrant, clinician_id: str, now: datetime = None):
    """Give this account its monthly hours if it has not had them this month.

    Created on demand rather than by a scheduled job: a job that fails to run
    would leave someone unable to record, and this way the grant appears the
    first time it is needed.
    """
    now = now or datetime.now(timezone.utc)
    start = month_start(now)
    existing = (HoursGrant.query
                .filter(HoursGrant.clinician_id == clinician_id,
                        HoursGrant.kind == MONTHLY,
                        HoursGrant.granted_at >= start)
                .first())
    if existing is not None:
        return existing
    grant = HoursGrant(clinician_id=clinician_id, kind=MONTHLY,
                       minutes=MONTHLY_MINUTES, used_minutes=0,
                       granted_at=now, expires_at=next_month_start(now))
    db.session.add(grant)
    db.session.commit()
    return grant


def grant_topup(db, HoursGrant, clinician_id: str, stripe_ref: str = None,
                now: datetime = None):
    """Credit a purchased block. Returns the grant, or None if this payment has
    already been credited — Stripe can deliver the same webhook more than once."""
    now = now or datetime.now(timezone.utc)
    if stripe_ref:
        already = HoursGrant.query.filter_by(stripe_ref=stripe_ref).first()
        if already is not None:
            return None
    grant = HoursGrant(clinician_id=clinician_id, kind=TOPUP,
                       minutes=TOPUP_MINUTES, used_minutes=0,
                       granted_at=now, expires_at=topup_expiry(now),
                       stripe_ref=stripe_ref)
    db.session.add(grant)
    db.session.commit()
    return grant


def _live_grants(HoursGrant, clinician_id: str, now: datetime):
    """Unexpired grants with time left, soonest expiry first.

    Order matters: spending the soonest-to-expire hours first is what stops
    someone losing a block they paid for while a longer-lived one sat unused.
    """
    rows = (HoursGrant.query
            .filter(HoursGrant.clinician_id == clinician_id)
            .order_by(HoursGrant.expires_at.asc(), HoursGrant.id.asc())
            .all())
    return [g for g in rows
            if _as_utc(g.expires_at) > now and (g.used_minutes or 0) < g.minutes]


def remaining_minutes(HoursGrant, clinician_id: str, now: datetime = None) -> int:
    """How much recording time this account has left."""
    now = now or datetime.now(timezone.utc)
    return sum(g.minutes - (g.used_minutes or 0)
               for g in _live_grants(HoursGrant, clinician_id, now))


def consume(db, HoursGrant, clinician_id: str, minutes: int,
            now: datetime = None) -> int:
    """Spend `minutes`, drawing from the soonest-expiring grant first.

    Returns how much could NOT be covered. A non-zero return means they recorded
    past their balance — possible because a recording in progress is only
    measured when it stops. Nothing is refused retroactively; the overspend just
    leaves the balance at zero and the next start is blocked.
    """
    now = now or datetime.now(timezone.utc)
    if minutes <= 0:
        return 0
    left = int(minutes)
    for grant in _live_grants(HoursGrant, clinician_id, now):
        spare = grant.minutes - (grant.used_minutes or 0)
        take = min(spare, left)
        grant.used_minutes = (grant.used_minutes or 0) + take
        left -= take
        if left <= 0:
            break
    db.session.commit()
    return left


def crossed_warning(before: int, after: int):
    """The warning threshold just passed, or None.

    Compares before/after so each threshold fires once, however long the
    recording was — a single 6-hour session that jumps from 7 hours left to 1
    reports the 5-hour warning rather than silently skipping it.
    """
    for threshold in WARN_AT_MINUTES:
        if before > threshold >= after:
            return threshold
    return None


def describe(minutes: int) -> str:
    """Plain words for a balance, for the screen and the emails."""
    if minutes <= 0:
        return "no recording time left"
    if minutes < 60:
        return f"{minutes} minutes left"
    hours = minutes // 60
    rest = minutes % 60
    if rest == 0:
        return f"{hours} hour{'s' if hours != 1 else ''} left"
    return f"{hours}h {rest}m left"
