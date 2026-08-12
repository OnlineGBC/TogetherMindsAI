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


def active_grants(CompAccess):
    """All grants, newest first — active and revoked (the list shows both)."""
    return CompAccess.query.order_by(CompAccess.id.desc()).all()
