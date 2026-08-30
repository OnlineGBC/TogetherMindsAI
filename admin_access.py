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

# Stripe takes roughly 2.9% + 30c, which on a $16 plan is about 4.7%. Below this
# a sale would cost more to make than it brings in, so a pair that leaves less is
# refused rather than saved and discovered later on an invoice.
MIN_KEPT_PCT = 5


def split_error(discount_pct, commission_pct):
    """Why this pair cannot be saved, or None if it is fine."""
    try:
        discount = int(discount_pct)
        commission = int(commission_pct)
    except (TypeError, ValueError):
        return "Enter both percentages as whole numbers."
    if not (1 <= discount <= 100):
        return "The discount must be between 1 and 100."
    if not (0 <= commission <= 100):
        return "The commission must be between 0 and 100."
    # A 100%-off code is fine as long as nobody is owed a share of it: no money
    # changes hands, so there is no card fee and nothing to lose. This is what
    # the free testing code is. Pair it with a commission and you would collect
    # nothing while owing someone — the worst case there is.
    if discount == 100:
        return ("A 100% off code cannot pay a commission — nothing is collected "
                "to pay it from.") if commission else None
    kept = 100 - discount - commission
    if kept < MIN_KEPT_PCT:
        return (f"That leaves you {kept}%, which does not cover the card fee. "
                f"Keep at least {MIN_KEPT_PCT}%.")
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


def stop_promo_code(db, PromoCode, row_id) -> bool:
    """Switch a code off in Stripe and here. Anyone already subscribed keeps their
    discount — Stripe leaves an applied discount on the subscription."""
    import billing
    row = db.session.get(PromoCode, row_id)
    if row is None or not row.active:
        return False
    billing.deactivate_promotion_code(row.promo_id)
    row.active = False
    db.session.commit()
    return True


def promo_code_uses(PromoCode) -> dict:
    """code → how many times Stripe says it has been redeemed."""
    import billing
    out = {}
    for row in PromoCode.query.all():
        out[row.code] = billing.promotion_code_uses(row.promo_id) if row.promo_id else None
    return out
