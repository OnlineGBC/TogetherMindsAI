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
# The one discount code the console manages.
#
# Stripe will not rename a promotion code, so "changing the code" is really
# "create the new one, switch the old one off". That happens here, in one step,
# so the console can show a single field and a single button.
# ---------------------------------------------------------------------------


def current_discount(db, DiscountCode):
    """The single row, created on first read with the starting code.

    Creating the row does NOT create anything in Stripe — `promo_id` stays NULL
    until an admin saves. A page view must never make live billing objects.
    """
    import billing
    row = DiscountCode.query.order_by(DiscountCode.id.desc()).first()
    if row is None:
        row = DiscountCode(code=billing.DEFAULT_DISCOUNT_CODE, promo_id=None,
                           active=True, updated_at=datetime.now(timezone.utc))
        db.session.add(row)
        db.session.commit()
    return row


def set_discount_code(db, DiscountCode, new_code: str, admin_email: str):
    """Point the discount at a new code. Returns the row.

    Raises on any Stripe failure, having changed nothing here — a half-done
    change must be visible rather than stored as if it had worked.
    """
    import billing
    new_code = (new_code or "").strip()
    if not billing.is_valid_code(new_code):
        raise ValueError("Use letters, numbers and dashes only.")

    row = current_discount(db, DiscountCode)
    if row.active and row.code == new_code and row.promo_id:
        return row                      # already exactly this; nothing to do

    promo = billing.create_promotion_code(new_code)   # raises if Stripe is unhappy
    old_promo_id = row.promo_id
    row.code = new_code
    row.promo_id = promo.id
    row.active = True
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = admin_email
    db.session.commit()
    # Only after the new one is safely stored — otherwise a failure here would
    # leave the account with no working code at all.
    billing.deactivate_promotion_code(old_promo_id)
    return row


def turn_off_discount(db, DiscountCode, admin_email: str):
    """Switch the current code off in Stripe and here. Returns the row."""
    import billing
    row = current_discount(db, DiscountCode)
    billing.deactivate_promotion_code(row.promo_id)
    row.active = False
    row.promo_id = None
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = admin_email
    db.session.commit()
    return row


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
