"""
admin_access.py
---------------
Comp access (full access without paying) and the admin 2-of-3 challenge.

Flask-free on purpose so every rule here is directly testable: hashing, code
issue/verify, the factor count, and the comp grant/revoke helpers. The HTTP
routes live in routes_admin.py.

Why emails are hashed: Clinician.email is Fernet-encrypted, which produces
different ciphertext each time, so `filter_by(email=...)` can never match. A
deterministic HMAC gives a stable lookup key AND lets an address be comped
before that person has ever signed up.
"""
import hmac
import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone, timedelta

import config

log = logging.getLogger(__name__)

CODE_DIGITS = 6
CHANNELS = ("email",)


def _hmac_key() -> bytes:
    """Key for the lookup hashes. SECRET_KEY is already required and rotated with
    the deployment; the hash is a lookup index, not a password store."""
    return (config.SECRET_KEY or "").encode() or b"insecure-dev-key"


def email_hash(email: str) -> str:
    """Stable lookup hash for an email address. Case- and space-insensitive."""
    normalised = (email or "").strip().lower()
    if not normalised:
        return ""
    return hmac.new(_hmac_key(), normalised.encode(), hashlib.sha256).hexdigest()


def code_hash(code: str) -> str:
    """Hash for a one-time code, so codes are never stored in the clear."""
    return hmac.new(_hmac_key(), (code or "").encode(), hashlib.sha256).hexdigest()


def is_admin(email: str) -> bool:
    """True when this address is configured as an admin."""
    return bool(email) and (email or "").strip().lower() in config.ADMIN_EMAILS


def new_code() -> str:
    """A fresh numeric one-time code, uniformly random."""
    upper = 10 ** CODE_DIGITS
    return str(secrets.randbelow(upper)).zfill(CODE_DIGITS)


def clean_secret(raw: str) -> str:
    """A base32 secret as stored, made safe to decode.

    Secret stores hand back exactly the bytes that were written, and a value piped
    in from a Windows shell arrives wrapped in a UTF-8 BOM and CRLF. str.strip()
    removes the CRLF but NOT the BOM — U+FEFF is not whitespace in Python — so the
    secret silently failed to base32-decode and the whole factor returned false
    with nothing in the logs. Strip the BOM explicitly, and any internal spacing
    some tools add when displaying a key.
    """
    return (raw or "").replace("﻿", "").replace(" ", "").strip()


def normalise_code(submitted: str) -> str:
    """Digits only. Authenticator apps display codes as "123 456", and that inner
    space survives .strip(), so a pasted or typed code with it would never verify.
    Also tolerates hyphens and any other separator a user might include."""
    return re.sub(r"\D", "", submitted or "")


def verify_totp(submitted: str) -> bool:
    """Check a code from the authenticator app. False when TOTP isn't configured
    or pyotp isn't installed — a missing factor must never count as a pass."""
    code = normalise_code(submitted)
    secret = clean_secret(config.ADMIN_TOTP_SECRET)
    if not (code and secret):
        return False
    try:
        import pyotp
    except ImportError:                              # pragma: no cover
        log.warning("pyotp not installed - TOTP factor unavailable")
        return False
    try:
        # valid_window=1 tolerates one 30s step of clock drift either way.
        return bool(pyotp.TOTP(secret).verify(code, valid_window=1))
    except Exception as exc:
        # Nearly always a secret that will not base32-decode. Say so loudly: this
        # failed silently once already and looked like "the code is wrong".
        log.warning("totp verify error (%s) - check ADMIN_TOTP_SECRET is clean "
                    "base32 with no BOM/newline", type(exc).__name__)
        return False


def totp_provisioning_uri(account: str) -> str:
    """otpauth:// URI for enrolling the authenticator app, or "" if unavailable."""
    secret = clean_secret(config.ADMIN_TOTP_SECRET)
    if not secret:
        return ""
    try:
        import pyotp
        return pyotp.TOTP(secret).provisioning_uri(
            name=account, issuer_name="TogetherMindsAI")
    except Exception:                                # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# One-time codes. `db` and `AdminAuthCode` are passed in so this module stays
# import-light and the tests can drive it directly.
# ---------------------------------------------------------------------------

def issue_code(db, AdminAuthCode, admin_email: str, channel: str) -> str:
    """Create and store a one-time code for `channel`, returning the plaintext
    code to send. Any earlier unused code on the same channel is retired first,
    so only the newest one works."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    now = datetime.now(timezone.utc)
    a_hash = email_hash(admin_email)

    (AdminAuthCode.query
     .filter(AdminAuthCode.admin_hash == a_hash,
             AdminAuthCode.channel == channel,
             AdminAuthCode.used_at.is_(None))
     .update({AdminAuthCode.used_at: now}, synchronize_session=False))

    code = new_code()
    db.session.add(AdminAuthCode(
        admin_hash=a_hash, channel=channel, code_hash=code_hash(code),
        created_at=now,
        expires_at=now + timedelta(minutes=config.ADMIN_CODE_TTL_MINUTES),
        attempts=0,
    ))
    db.session.commit()
    return code


def verify_code(db, AdminAuthCode, admin_email: str, channel: str, submitted: str) -> bool:
    """Check a one-time code. Consumes it on success. Counts the attempt either
    way, and refuses once the attempt budget is spent."""
    code = normalise_code(submitted)
    if not code:
        return False
    now = datetime.now(timezone.utc)
    row = (AdminAuthCode.query
           .filter(AdminAuthCode.admin_hash == email_hash(admin_email),
                   AdminAuthCode.channel == channel,
                   AdminAuthCode.used_at.is_(None))
           .order_by(AdminAuthCode.id.desc())
           .first())
    if row is None:
        return False
    expires = row.expires_at
    if expires.tzinfo is None:                       # naive when read back from the DB
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now or (row.attempts or 0) >= config.ADMIN_CODE_MAX_ATTEMPTS:
        return False

    row.attempts = (row.attempts or 0) + 1
    ok = hmac.compare_digest(row.code_hash, code_hash(code))
    if ok:
        row.used_at = now                            # single use
    db.session.commit()
    return ok


def count_factors(db, AdminAuthCode, admin_email: str,
                  totp: str = "", email_code: str = "") -> int:
    """How many of the two factors verified. Both are checked (no short-circuit),
    so one wrong code does not mask a correct one."""
    passed = 0
    if verify_totp(totp):
        passed += 1
    if verify_code(db, AdminAuthCode, admin_email, "email", email_code):
        passed += 1
    return passed


def challenge_passed(db, AdminAuthCode, admin_email: str,
                     totp: str = "", email_code: str = "") -> bool:
    """True when enough factors verified (either one, by default)."""
    return count_factors(db, AdminAuthCode, admin_email,
                         totp, email_code) >= config.ADMIN_FACTORS_REQUIRED


# ---------------------------------------------------------------------------
# Comp grants
# ---------------------------------------------------------------------------

def has_comp_access(CompAccess, email: str) -> bool:
    """True when this address holds an active comp grant."""
    h = email_hash(email)
    if not h:
        return False
    return CompAccess.query.filter(CompAccess.email_hash == h,
                                   CompAccess.revoked_at.is_(None)).first() is not None


def grant(db, CompAccess, email: str, note: str, added_by: str):
    """Grant (or re-activate) comp access for an address. Returns the row, or
    None when the address is unusable."""
    normalised = (email or "").strip().lower()
    if "@" not in normalised:
        return None
    now = datetime.now(timezone.utc)
    row = CompAccess.query.filter_by(email_hash=email_hash(normalised)).first()
    if row is None:
        row = CompAccess(email_hash=email_hash(normalised), email=normalised,
                         note=(note or "")[:200], added_by=added_by, created_at=now)
        db.session.add(row)
    else:                                            # re-activate a revoked grant
        row.revoked_at = None
        row.note = (note or row.note or "")[:200]
        row.added_by = added_by
    db.session.commit()
    return row


def revoke(db, CompAccess, row_id: int) -> bool:
    """Revoke a grant. Keeps the row so the history survives for audit."""
    row = db.session.get(CompAccess, row_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Role administration. A role decides what the app may claim and store about
# someone's work, so it is set once at sign-up and only an admin may change it.
# ---------------------------------------------------------------------------

ACCOUNT_LIST_LIMIT = 200


def list_accounts(Clinician, limit: int = ACCOUNT_LIST_LIMIT):
    """(rows, total) — the newest accounts, plus how many exist in all.

    Returns the total as well as the page so the console can say plainly when it
    is not showing everything. Silently truncating would read as "this is all of
    them", which is exactly the wrong impression on an admin screen.
    """
    total = Clinician.query.count()
    rows = (Clinician.query
            .order_by(Clinician.last_login_at.desc().nullslast(),
                      Clinician.created_at.desc())
            .limit(limit)
            .all())
    return rows, total


def set_role(db, Clinician, clinician_id: str, new_role: str):
    """Change an account's role. Returns (old_role, new_role) on success, or None
    if the account is unknown, the role is invalid, or it is already set to that."""
    import roles
    if not roles.is_valid(new_role):
        return None
    clin = db.session.get(Clinician, clinician_id)
    if clin is None:
        return None
    old_role = clin.role
    if old_role == new_role:
        return None
    clin.role = new_role
    db.session.commit()
    return (old_role, new_role)


# ---------------------------------------------------------------------------
# Switching an account off. Deliberately NOT deletion: an account's id is also
# written into their therapy sessions and state licence certificates, which are
# client records the practice is required to retain. Removing the account would
# orphan those. Disabling takes away access and leaves every record intact.
# ---------------------------------------------------------------------------

# Shown on the console and written into the audit log on every change, so the
# page and the permanent record can never drift apart. One source of truth.
DISABLE_NOTICE = (
    "Disabling blocks sign-in and ends any session already open. Nothing is "
    "deleted — their therapy sessions, licence certificates, recording hours and "
    "permissions all stay exactly as they are. Enable the account at any time to "
    "restore access."
)


def set_disabled(db, Clinician, clinician_id: str, disabled: bool):
    """Switch an account off or back on.

    Returns the new state (True = disabled) on success, or None if the account is
    unknown or is already in that state — so the caller can say "no change made"
    rather than log an event that did nothing.
    """
    clin = db.session.get(Clinician, clinician_id)
    if clin is None:
        return None
    already = clin.disabled_at is not None
    if already == disabled:
        return None
    clin.disabled_at = datetime.now(timezone.utc) if disabled else None
    db.session.commit()
    return disabled


def is_disabled(db, Clinician, clinician_id: str) -> bool:
    """True when this account has been switched off by an admin.

    Used on every request for a signed-in clinician, so it reads one row by
    primary key and nothing else.
    """
    if not clinician_id:
        return False
    clin = db.session.get(Clinician, clinician_id)
    return bool(clin is not None and clin.disabled_at is not None)


def active_grants(CompAccess):
    """All grants, newest first — active and revoked (the list shows both)."""
    return CompAccess.query.order_by(CompAccess.id.desc()).all()


# ---------------------------------------------------------------------------
# Reading the audit log.
#
# Everything was already recorded; there was simply no way to look at it without
# database access. Read-only: this module never writes to audit_logs, and no
# route here offers a delete — the table is append-only and hash-chained.
# ---------------------------------------------------------------------------

# What an admin usually wants: the things done FROM this console, plus the
# attempts to get into it. The rest of the log is session traffic — thousands of
# rows a day, and noise on this page — so it is behind a toggle rather than the
# default.
ADMIN_EVENT_TYPES = (
    "comp_access_granted", "comp_access_revoked",
    "role_changed", "account_disabled", "account_enabled",
    "discount_code_set", "discount_code_off",
    "admin_code_sent", "admin_challenge",
    "clinician_login_blocked",
)

AUDIT_PAGE_LIMIT = 100


def _accounts_matching(Clinician, needle: str) -> list:
    """Ids of accounts whose email contains `needle`.

    Done in Python, not SQL: Clinician.email is Fernet-encrypted with a
    non-deterministic cipher, so every row's ciphertext differs and a LIKE could
    never match. The account list is small (see ACCOUNT_LIST_LIMIT), so reading
    it and decrypting is cheap.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    ids = []
    for cid in _account_ids(Clinician):
        if cid.lower().startswith(needle):
            ids.append(cid)
            continue
        email = (_email_of(Clinician, cid) or "").lower()
        if needle and needle in email:
            ids.append(cid)
    return ids


def _account_ids(Clinician) -> list:
    """Account ids only — no email column, so nothing is decrypted here."""
    return [row[0] for row in
            Clinician.query.with_entities(Clinician.id).limit(ACCOUNT_LIST_LIMIT).all()]


def _email_of(Clinician, clinician_id: str):
    """One account's email, or None if it will not decrypt.

    Loaded one row at a time on purpose. Decryption happens while the row is
    being read, so a single account encrypted under an older key makes a BULK
    query raise — which would take the whole page down instead of skipping one
    unreadable name.
    """
    try:
        row = Clinician.query.filter(Clinician.id == clinician_id).first()
        return row.email if row is not None else None
    except Exception:
        return None


def _parse_day(value: str, end_of_day: bool = False):
    """A YYYY-MM-DD box into a datetime, or None if empty/unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59)
    return day.replace(tzinfo=timezone.utc)


def search_audit(db, AuditLog, Clinician, *, who="", event="", date_from="",
                 date_to="", text="", admin_only=True, limit=AUDIT_PAGE_LIMIT):
    """(rows, truncated) — the newest matching entries, plus whether more exist.

    Every filter is optional and they combine. `who` matches an account's email
    or the start of its id; `text` matches the event name or the stored details.
    """
    q = AuditLog.query

    if event:
        q = q.filter(AuditLog.event_type == event)
    elif admin_only:
        q = q.filter(AuditLog.event_type.in_(ADMIN_EVENT_TYPES))

    if who:
        ids = _accounts_matching(Clinician, who)
        if not ids:
            return [], False        # nobody matched, so nothing can match
        # The actor OR the person acted upon: a role change records the admin as
        # user_id and the target inside details, and both readings of "who" are
        # what someone means when they type a name.
        q = q.filter(db.or_(AuditLog.user_id.in_(ids),
                            *[AuditLog.details.contains(i) for i in ids]))

    start = _parse_day(date_from)
    end = _parse_day(date_to, end_of_day=True)
    if start:
        q = q.filter(AuditLog.timestamp >= start)
    if end:
        q = q.filter(AuditLog.timestamp <= end)

    if text:
        needle = f"%{text.strip()}%"
        q = q.filter(db.or_(AuditLog.event_type.ilike(needle),
                            AuditLog.details.ilike(needle)))

    # One more than asked for, so the page can say plainly that it is not showing
    # everything rather than silently truncating.
    rows = q.order_by(AuditLog.id.desc()).limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


def audit_event_types(AuditLog) -> list:
    """Every event name present in the log, for the filter dropdown."""
    return sorted(r[0] for r in db_distinct(AuditLog))


def db_distinct(AuditLog):
    return AuditLog.query.with_entities(AuditLog.event_type).distinct().all()


def account_labels(Clinician) -> dict:
    """id → something a human can read, for naming the ids in the log."""
    out = {}
    for cid in _account_ids(Clinician):
        out[cid] = _email_of(Clinician, cid) or f"no email · {cid[:8]}"
    return out


# ---------------------------------------------------------------------------
# Discount codes, with or without a partner behind them.
#
# The two percentages are NOT symmetrical. The discount is real — Stripe applies
# it at checkout. The commission is a number we store and pay by hand, so the
# report is the only thing between it and someone being paid wrongly.
# ---------------------------------------------------------------------------

def split_error(discount_pct, commission_pct):
    """Why this pair cannot be saved, or None if it is fine.

    Only what the numbers have to be to work at all: whole numbers, and a
    discount in the range Stripe accepts for percent_off. Whatever split the
    admin wants between the customer and the partner is theirs to choose — the
    console shows what is left over and does not argue with it.
    """
    try:
        discount = int(discount_pct)
        commission = int(commission_pct)
    except (TypeError, ValueError):
        return "Enter both percentages as whole numbers."
    if not (1 <= discount <= 100):
        return "The discount must be between 1 and 100."   # Stripe's own range
    if not (0 <= commission <= 100):
        return "The commission must be between 0 and 100."
    return None


def create_promo_code(db, PromoCode, *, label, email, discount_pct, commission_pct,
                      max_uses=None, admin_email=None):
    """Add a discount code and create it in Stripe. Returns the row.

    `commission_pct` of 0 means a plain discount with nobody to pay — the 100%-off
    testing code is exactly that. Anything above 0 makes it a partner's code.

    Raises ValueError for a bad split, and lets a Stripe failure raise: a row here
    with no code there would hand someone a code that does not work, so nothing is
    stored unless Stripe accepted it.
    """
    import billing
    label = (label or "").strip()
    if not label:
        raise ValueError("Enter a label — a partner's name, or what the code is for.")
    problem = split_error(discount_pct, commission_pct)
    if problem:
        raise ValueError(problem)

    uses = None
    if str(max_uses or "").strip():
        try:
            uses = int(max_uses)
        except (TypeError, ValueError):
            raise ValueError("Max uses must be a whole number.")
        if uses < 1:
            raise ValueError("Max uses must be at least 1.")

    code = billing.suggest_code(label)
    promo = billing.create_promo_code(code, int(discount_pct), uses)  # raises if refused
    row = PromoCode(
        label=label, email=(email or "").strip() or None, code=code,
        discount_pct=int(discount_pct), commission_pct=int(commission_pct),
        max_uses=uses, promo_id=promo.id, active=True,
        created_at=datetime.now(timezone.utc), created_by=admin_email,
    )
    db.session.add(row)
    db.session.commit()
    return row


def list_promo_codes(PromoCode) -> list:
    return PromoCode.query.order_by(PromoCode.id.desc()).all()


def edit_promo_code(db, PromoCode, row_id, *, label=None, email=None,
                    commission_pct=None, active=None):
    """Change what CAN be changed. Returns the row, or None if unknown.

    Only three of the six fields are ours to edit. Stripe's update endpoint takes
    `active`, `metadata` and `restrictions` and nothing else, so the code itself,
    the discount and the max-uses cap are fixed at creation — to change one of
    those, delete the code and add another.
    """
    import billing
    row = db.session.get(PromoCode, row_id)
    if row is None:
        return None

    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("Enter a label — a partner's name, or what the code is for.")
        row.label = label
    if email is not None:
        row.email = email.strip() or None
    if commission_pct is not None and str(commission_pct).strip():
        try:
            commission = int(commission_pct)
        except (TypeError, ValueError):
            raise ValueError("Enter the commission as a whole number.")
        if not (0 <= commission <= 100):
            raise ValueError("The commission must be between 0 and 100.")
        row.commission_pct = commission

    if active is not None and bool(active) != bool(row.active):
        # Stripe first: if it refuses, the row must not claim a state Stripe does
        # not agree with.
        if active:
            billing.reactivate_promotion_code(row.promo_id)
        else:
            billing.deactivate_promotion_code(row.promo_id)
        row.active = bool(active)

    db.session.commit()
    return row


def delete_promo_code(db, PromoCode, row_id) -> bool:
    """Remove a code. Returns False if it was already gone.

    Stripe has no delete for a promotion code, so it is switched off there and the
    row is removed here — from the console's point of view it is gone and the code
    stops working. Two things follow, and both are stated on the page:
    anyone already subscribed KEEPS their discount (Stripe leaves an applied
    discount on the subscription), and the record of what a partner is owed goes
    with the row, which is why switching a code off is offered as well.
    """
    import billing
    row = db.session.get(PromoCode, row_id)
    if row is None:
        return False
    billing.deactivate_promotion_code(row.promo_id)
    db.session.delete(row)
    db.session.commit()
    return True


def promo_code_uses(PromoCode) -> dict:
    """code → how many times Stripe says it has been redeemed."""
    import billing
    out = {}
    for row in PromoCode.query.all():
        out[row.code] = billing.promotion_code_uses(row.promo_id) if row.promo_id else None
    return out


# ---------------------------------------------------------------------------
# Usage alerts.
#
# Stripe counts redemptions on the code itself, so a sweep asks it rather than
# reacting to a webhook: the count is the thing being watched, and asking for it
# cannot be wrong about the payload shape.
#
# Three warnings, on fixed rules rather than a judgement call — an email that
# fires on someone's opinion is one nobody can predict or debug.
# ---------------------------------------------------------------------------

ALERT_NEARLY = "nearly_spent"
ALERT_SPENT = "spent"
ALERT_BURST = "burst"


def alerts_for(row, uses, *, nearly_pct, burst_per_sweep, burst_recent=False):
    """Which warnings this code has earned right now, as a list.

    Pure: no database, no email, no clock. Everything the decision needs is an
    argument, so every rule below is directly testable.
    """
    out = []
    if uses is None:                       # Stripe did not answer — say nothing
        return out
    since = uses - (row.last_seen_uses or 0)

    if row.max_uses:
        # Reaching the cap rules out "nearly there" completely — not just on the
        # sweep that reports it. Chaining these as if/elif on the SENT flags meant
        # a code already flagged as spent went on to send "nearly used up" on the
        # very next sweep, which is both wrong and a second email.
        if uses >= row.max_uses:
            if not row.alerted_spent:
                out.append(ALERT_SPENT)
        elif not row.alerted_nearly and uses >= (row.max_uses * nearly_pct) / 100:
            out.append(ALERT_NEARLY)

    # A burst can repeat — it is about speed, not a threshold reached once — so it
    # is throttled by time elsewhere rather than by a one-shot flag.
    if since >= burst_per_sweep and not burst_recent:
        out.append(ALERT_BURST)
    return out


def alert_message(kind, row, uses):
    """(subject, body) for one warning, in plain words."""
    left = (row.max_uses - uses) if row.max_uses else None
    if kind == ALERT_SPENT:
        return (f"Discount code {row.code} has been fully used",
                f"The code {row.code} ({row.label}) has been used all "
                f"{row.max_uses} times and has stopped working.\n\n"
                f"Nobody can sign up with it now. Make a new code if you want to "
                f"keep going.")
    if kind == ALERT_NEARLY:
        return (f"Discount code {row.code} is nearly used up",
                f"The code {row.code} ({row.label}) has been used {uses} of "
                f"{row.max_uses} times. {left} left.\n\n"
                f"It will stop working when the last one is used.")
    return (f"Discount code {row.code} is being used unusually fast",
            f"The code {row.code} ({row.label}) was used "
            f"{uses - (row.last_seen_uses or 0)} times in the last hour.\n\n"
            f"That is what a code being passed around looks like. If that was not "
            f"expected, switch it off in the admin console.")


def alert_recipients(row, admin_emails) -> list:
    """The partner, plus every admin. A code with no partner goes to admins only."""
    out = [a for a in (admin_emails or []) if a]
    if row.email and row.email not in out:
        out.insert(0, row.email)
    return out


def sweep_promo_alerts(db, PromoCode, *, send, now, nearly_pct, burst_per_sweep,
                       admin_emails, uses_of, burst_quiet_hours=6):
    """Check every live code and send what it has earned. Returns what was sent.

    `send` and `uses_of` are passed in rather than imported: the rules above are
    the part worth testing, and neither Stripe nor SMTP should be reached to do it.

    A code whose email fails is NOT marked as alerted, so the next sweep tries
    again. Marking it sent on a failure would lose the warning permanently, and a
    warning nobody receives is worse than a duplicate.
    """
    sent = []
    for row in PromoCode.query.filter_by(active=True).all():
        try:
            uses = uses_of(row)
        except Exception:
            continue                       # Stripe unreachable — try next sweep
        if uses is None:
            continue

        quiet = False
        if row.last_burst_alert is not None:
            last = row.last_burst_alert
            if last.tzinfo is None:        # naive when read back from the DB
                last = last.replace(tzinfo=timezone.utc)
            quiet = (now - last) < timedelta(hours=burst_quiet_hours)

        for kind in alerts_for(row, uses, nearly_pct=nearly_pct,
                               burst_per_sweep=burst_per_sweep, burst_recent=quiet):
            subject, body = alert_message(kind, row, uses)
            to = alert_recipients(row, admin_emails)
            if not to:
                continue
            try:
                send(to, subject, body)
            except Exception:
                log.warning("promo alert email failed for %s (%s) — will retry",
                            row.code, kind)
                continue                   # deliberately not marked as sent
            if kind == ALERT_SPENT:
                row.alerted_spent = True
            elif kind == ALERT_NEARLY:
                row.alerted_nearly = True
            else:
                row.last_burst_alert = now
            sent.append((row.code, kind))

        # Always last: the next sweep measures the jump from here, and it must move
        # even when nothing was sent, or one quiet hour would look like a burst.
        row.last_seen_uses = uses
    db.session.commit()
    return sent


# ---------------------------------------------------------------------------
# The payout report.
#
# The commission exists ONLY in our database — Stripe knows nothing about it — so
# this report is the only thing standing between that number and someone being
# paid the wrong amount. Two habits follow from that, and both are deliberate:
#
#   1. Every money figure comes from what Stripe COLLECTED. The customer paid a
#      discounted amount, so list price would overpay every partner, every month.
#   2. Each referral carries its own copy of the partner and the percentage. The
#      code can be edited or deleted afterwards and the report still answers.
# ---------------------------------------------------------------------------

# A partner earns for one year from the referred customer's first collected
# payment. Payments after that earn nothing.
PAYOUT_WINDOW_DAYS = 365


def commission_cents(amount_cents, pct) -> int:
    """A partner's share of a collected payment, in whole cents.

    Rounded half up rather than with round(), which rounds .5 to the nearest EVEN
    number — so 2.5 cents would become 2 and 3.5 would become 4, quietly and
    inconsistently. Money is expected to round up on the half.
    """
    amount = int(amount_cents or 0)
    share = int(pct or 0)
    if amount <= 0 or share <= 0:
        return 0
    return (amount * share + 50) // 100


def promo_id_from_checkout(obj) -> str:
    """The Stripe promotion code id used at this checkout, or "".

    Stripe sends `discounts` as a list of {coupon, promotion_code}, and each value
    may be a plain id string OR an expanded object depending on the account's API
    version. Both shapes are read here rather than assumed, because guessing wrong
    means a referral is silently never recorded.
    """
    for entry in (obj.get("discounts") or []):
        promo = entry.get("promotion_code") if isinstance(entry, dict) else None
        if isinstance(promo, dict):
            promo = promo.get("id")
        if promo:
            return str(promo)
    return ""


def referral_from_checkout(db, Referral, PromoCode, *, promo_id, clinician_id,
                           customer_id, now):
    """Record who referred this customer. Returns the row, or None.

    None when no code was used, the code is not one of ours, or this clinician is
    already someone's referral — the FIRST code they used is who referred them.

    The partner's name, email and share are copied in. Nothing here reads
    promo_codes again afterwards.
    """
    if not (promo_id and clinician_id):
        return None
    existing = Referral.query.filter_by(clinician_id=clinician_id).first()
    if existing is not None:
        # Learn the customer id if checkout is where it first appears — later
        # payments name the customer and nothing else, so without it the money
        # could never be matched back.
        if customer_id and not existing.stripe_customer_id:
            existing.stripe_customer_id = customer_id
            db.session.commit()
        return None
    code_row = PromoCode.query.filter_by(promo_id=promo_id).first()
    if code_row is None:
        return None
    row = Referral(
        code=code_row.code, partner_label=code_row.label,
        partner_email=code_row.email, commission_pct=code_row.commission_pct or 0,
        clinician_id=clinician_id, stripe_customer_id=customer_id or None,
        first_payment_at=None, earns_until=None, created_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _aware(value):
    """A datetime read back from the database, made comparable. Timestamps come
    back naive from both SQLite and Postgres, and comparing one to an aware `now`
    raises."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def record_payment(db, Referral, ReferralPayment, *, customer_id, stripe_ref,
                   amount_cents, currency, paid_at):
    """Record a payment Stripe collected from a referred customer. Returns the row,
    or None when there is nothing to record.

    None when this customer is nobody's referral, the same payment has already been
    recorded, or nothing was actually collected.

    A payment of 0 is ignored entirely: a 100%-off code collects nothing, and a
    year of earning must not start on a charge that never happened.
    """
    if not (customer_id and stripe_ref):
        return None
    amount = int(amount_cents or 0)
    if amount <= 0:
        return None
    if ReferralPayment.query.filter_by(stripe_ref=stripe_ref).first() is not None:
        return None                        # webhook delivered twice
    referral = Referral.query.filter_by(stripe_customer_id=customer_id).first()
    if referral is None:
        return None

    if referral.first_payment_at is None:
        # The clock starts on the first money that actually arrived.
        referral.first_payment_at = paid_at
        referral.earns_until = paid_at + timedelta(days=PAYOUT_WINDOW_DAYS)

    ends = _aware(referral.earns_until)
    in_window = bool(ends is None or paid_at <= ends)
    row = ReferralPayment(
        referral_id=referral.id, stripe_ref=stripe_ref, amount_cents=amount,
        currency=(currency or "").lower() or None, paid_at=paid_at,
        commission_cents=(commission_cents(amount, referral.commission_pct)
                          if in_window else 0),
        in_window=in_window,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _referral_ids(Referral, *, code=None) -> list:
    """Referral ids only — no encrypted column, so nothing is decrypted here."""
    q = Referral.query.with_entities(Referral.id)
    if code:
        q = q.filter(Referral.code == code)
    return [r[0] for r in q.order_by(Referral.id.desc()).all()]


def _referral_row(Referral, referral_id):
    """One referral, or None if it will not decrypt.

    Loaded one row at a time for the same reason as _email_of: decryption happens
    while the row is read, so a single row encrypted under an older key makes a
    BULK query raise — taking the whole page down instead of skipping one
    unreadable name.
    """
    try:
        return Referral.query.filter(Referral.id == referral_id).first()
    except Exception:
        return None


def payout_report(db, Referral, ReferralPayment, Clinician, *,
                  date_from="", date_to=""):
    """What each partner is owed, per referral. Returns (partners, totals).

    `partners` is a list of dicts, one per code, each holding its referrals. The
    money columns count only payments collected inside the date range, which is
    what a payout run means. Sign-ups are listed either way, so a referral that
    paid nothing this month is visible rather than missing.

    Deliberately NOT capped. Every other list on this console shows the most recent
    N and says so, but a payout report that quietly stopped short would underpay
    someone.
    """
    start = _parse_day(date_from)
    end = _parse_day(date_to, end_of_day=True)

    groups = {}
    for rid in _referral_ids(Referral):
        row = _referral_row(Referral, rid)
        if row is None:
            continue                       # unreadable row, not a dead page

        q = ReferralPayment.query.filter(ReferralPayment.referral_id == row.id)
        if start:
            q = q.filter(ReferralPayment.paid_at >= start)
        if end:
            q = q.filter(ReferralPayment.paid_at <= end)
        payments = q.order_by(ReferralPayment.paid_at.asc()).all()

        collected = sum(p.amount_cents or 0 for p in payments)
        owed = sum(p.commission_cents or 0 for p in payments)
        group = groups.setdefault(row.code, {
            "code": row.code, "label": row.partner_label,
            "email": row.partner_email, "commission_pct": row.commission_pct,
            "referrals": [], "collected_cents": 0, "owed_cents": 0,
            "currencies": set(),
        })
        group["referrals"].append({
            "who": _email_of(Clinician, row.clinician_id) or f"account {row.clinician_id[:8]}",
            "clinician_id": row.clinician_id,
            "signed_up": row.created_at,
            "first_payment_at": row.first_payment_at,
            "earns_until": row.earns_until,
            "commission_pct": row.commission_pct,
            "payments": len(payments),
            "expired": sum(1 for p in payments if not p.in_window),
            "collected_cents": collected,
            "owed_cents": owed,
        })
        group["collected_cents"] += collected
        group["owed_cents"] += owed
        group["currencies"].update(p.currency for p in payments if p.currency)

    partners = sorted(groups.values(), key=lambda g: (-g["owed_cents"], g["label"]))
    for group in partners:
        group["currencies"] = sorted(group["currencies"])
    totals = {
        "collected_cents": sum(g["collected_cents"] for g in partners),
        "owed_cents": sum(g["owed_cents"] for g in partners),
        "referrals": sum(len(g["referrals"]) for g in partners),
    }
    return partners, totals


def money(cents) -> str:
    """Cents as dollars, for the screen. Negative is impossible here, so there is
    no sign to handle."""
    return f"${(int(cents or 0)) / 100:,.2f}"
