import os
import config  # loads .env and exposes typed constants

# eventlet must be monkey-patched before any other imports when used
if config.ASYNC_MODE == "eventlet":
    import eventlet
    eventlet.monkey_patch()

import base64
import re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import io
import smtplib
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response, flash, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, join_room, emit
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.exceptions import InvalidSignature

from models import db, User, ChatMessage, Exercise, RateLimitEntry, TherapySession, AuditLog, Clinician, ClientAccount, SessionParticipant, init_encryption
from authlib.integrations.flask_client import OAuth
from ai_therapist import detect_crisis, CRISIS_RESPONSE
import copilot
from audit import log_event
from session_id import generate_session_id, normalise_join_input, rejoin_format_hint, rejoin_placeholder
from log_filter import install_log_filter

install_log_filter()   # redact PHI-bearing fields from all log output (finding 4.3)

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY or os.environ.get("SECRET_KEY", "dev-fallback-key")
app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = config.SQLALCHEMY_ENGINE_OPTIONS
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Session cookie hardening (HIPAA / finding 3.4)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.IS_PRODUCTION   # True on Cloud Run (HTTPS); False on localhost/test
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

db.init_app(app)

# Per-IP rate limiter (finding 3.9).
# On Cloud Run, the real client IP arrives in X-Forwarded-For; using
# PROXIES_COUNT=1 tells flask-limiter to trust the first forwarded address.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],                          # no global default — limits are per-route only
    storage_uri="memory://",                    # in-process; resets on restart (acceptable for rate limiting)
    headers_enabled=True,                       # send X-RateLimit-* headers so clients can back off
)
app.config["RATELIMIT_PROXIES_COUNT"] = 1       # trust one proxy hop (Cloud Run load balancer)

socketio = SocketIO(
    app,
    async_mode=config.ASYNC_MODE,
    cors_allowed_origins=config.CORS_ALLOWED_ORIGINS,
    # Disable WebSocket upgrade when running under werkzeug's dev server.
    # werkzeug does not support WebSocket; upgrade attempts cause a 500 error
    # (AssertionError: write() before start_response).  In production the
    # async_mode is eventlet, which handles WebSocket natively, so upgrades
    # are allowed there.
    allow_upgrades=not (config.FLASK_DEBUG or config.IS_TESTING),
)

_RATE_WINDOW     = config.RATE_WINDOW_SECONDS
_RATE_MAX_MSGS   = config.RATE_MAX_MESSAGES
_MAX_MSG_LEN     = config.MAX_MESSAGE_LENGTH
# Therapist-led sessions are clinical records, retained ~6 years (HIPAA § 164.312(b)).
# The expired-session purge job sweeps any row whose retention_expires_at has passed
# (e.g. legacy AI-led sessions expired by the one-time migration below).
_CLINICAL_RETENTION_DELTA = timedelta(days=2192)   # ~6 years

# ---------------------------------------------------------------------------
# Clinician OAuth (OpenID Connect) — Google & Microsoft. Registered even when
# credentials are unset (the /login buttons just won't work until they are set).
# ---------------------------------------------------------------------------
oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid"},
)
oauth.register(
    name="microsoft",
    client_id=config.MICROSOFT_CLIENT_ID,
    client_secret=config.MICROSOFT_CLIENT_SECRET,
    server_metadata_url=(
        f"https://login.microsoftonline.com/{config.MICROSOFT_TENANT}/v2.0/.well-known/openid-configuration"
    ),
    client_kwargs={"scope": "openid"},
)
_OAUTH_PROVIDERS = ("google", "microsoft")

# Azure multi-tenant ("common"/"organizations"/"consumers") publishes a discovery
# issuer templated as https://login.microsoftonline.com/{tenantid}/v2.0, but the
# real id_token substitutes the signing tenant's GUID — so Authlib's default
# "iss must equal metadata issuer" check rejects every Microsoft sign-in. Validate
# the way Microsoft documents instead: the issuer must equal the template with
# {tenantid} replaced by the token's own `tid` claim.
_MS_ISS_RE = re.compile(r"^https://login\.microsoftonline\.com/[0-9a-fA-F-]{36}/v2\.0$")


def _validate_ms_issuer(claims, value):
    """Return True if `value` is a valid Microsoft tenant issuer for this token."""
    if not value:
        return False
    tid = claims.get("tid")
    if tid:
        return value == f"https://login.microsoftonline.com/{tid}/v2.0"
    # No tid claim — fall back to accepting a well-formed tenant issuer.
    return bool(_MS_ISS_RE.match(value))



# In-memory maps (ephemeral — reset on restart, which is acceptable for these)
room_mode: dict = {}
room_participants: dict = defaultdict(set)   # session_id → set of user_ids
sid_to_user: dict = {}                        # SocketIO SID → user_id
sid_to_session: dict = {}                     # SocketIO SID → session_id

# Display name tracking — all ephemeral, reset on server restart
session_display_names: dict = {}  # session_id → {user_id: display_name}
session_taken_names: dict   = {}  # session_id → set of lowercased names (permanent for session lifetime)
session_joined_users: dict  = {}  # session_id → ordered list of user_ids (join order, for default names)

# Therapist co-pilot state — all ephemeral, repopulated on join from the DB.
session_therapist_id: dict    = {}  # session_id → therapist user_id (set only for therapist-led sessions)
session_therapist_notes: dict = {}  # session_id → list[str] of the therapist's private notes (co-pilot context)
session_recent_cards: dict    = {}  # session_id → list[str] of recently shown card texts (dedup memory)


def _record_participation(session_id: str, user_id: str) -> None:
    """Idempotently record that `user_id` joined `session_id`.

    Lets "my sessions" surface a session for a signed-in client even if they
    never spoke. Safe to call on every (re)join — the unique constraint plus the
    existence check keep it to one row per (session, user). Never raises."""
    if not session_id or not user_id:
        return
    try:
        exists = (
            db.session.query(SessionParticipant.id)
            .filter_by(session_id=session_id, user_id=user_id)
            .first()
        )
        if exists:
            return
        db.session.add(SessionParticipant(
            session_id=session_id, user_id=user_id,
            joined_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
    except Exception as exc:
        # Most likely a race on the unique constraint — already recorded.
        db.session.rollback()
        app.logger.debug("participation record skipped (%s)", type(exc).__name__)


def _therapist_room(session_id: str) -> str:
    """Private SocketIO room that only the session's therapist joins.

    Co-pilot cards are emitted here, never to the main session room, so clients
    are structurally unable to receive them (they are never added to this room).
    """
    return f"{session_id}::therapist"


def _build_transcript(session_id: str, limit: int = 20) -> str:
    """Build a speaker-labelled transcript of the last `limit` messages for the co-pilot."""
    msgs = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    therapist_id = session_therapist_id.get(session_id)
    lines = []
    for m in msgs[-limit:]:
        if m.user_id == therapist_id:
            who = "Therapist"
        elif m.user_id == "AI":
            who = "AI"
        else:
            who = m.display_name or "Client"
        lines.append(f"{who}: {m.text}")
    return "\n".join(lines)


def _run_copilot(session_id: str, mode: str, trigger_text: str = None) -> None:
    """Generate co-pilot cards and emit them to the therapist-only room.

    `trigger_text`, when given, is the latest client utterance — used for the
    zero-latency keyword risk check. Never raises into the caller.
    """
    try:
        transcript = _build_transcript(session_id)
        notes = "\n".join(session_therapist_notes.get(session_id, []))
        cards = []
        if trigger_text:
            cards.extend(copilot.build_risk_cards(trigger_text))
        cards.extend(copilot.build_reference_cards(transcript))
        cards.extend(copilot.generate_suggestions(transcript, mode=mode, therapist_notes=notes))

        recent = session_recent_cards.setdefault(session_id, [])
        cards = copilot.dedupe_cards(cards, recent)
        if not cards:
            return
        for c in cards:
            recent.append(c["text"])
        del recent[:-30]   # cap dedup memory

        socketio.emit("suggestion_cards", {"cards": cards}, to=_therapist_room(session_id))
        log_event("suggestions_generated", session_id=session_id, count=len(cards))
    except Exception as e:
        app.logger.error("copilot error: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Session modes — solo / couple / group are one multi-user realtime engine that
# differs only in presentation and the default participant-name scheme. This
# table centralises those per-mode strings; the room route and template read
# from it instead of branching. `mode` stays authoritative session data, so a
# couple session is always rendered (and advised on) as a couple.
# ---------------------------------------------------------------------------
MODE_CONFIG = {
    "solo": {
        "label": "1:1 Session",
        "icon": "person-fill",
        "placeholder": "Type a message…",
        "name_prefix": "Solo",
    },
    "couple": {
        "label": "Couple Check-in",
        "icon": "people-fill",
        "placeholder": "Type a message…",
        "name_prefix": "Partner",
    },
    "group": {
        "label": "Group Circle",
        "icon": "person-lines-fill",
        "placeholder": "Share with the group…",
        "name_prefix": "GroupMember",
    },
}


def _default_display_name(mode: str, position: int) -> str:
    """Return the default display name for a participant given their join position."""
    prefix = MODE_CONFIG.get(mode, {}).get("name_prefix", "Participant")
    return f"{prefix}{position}"


# Per-mode client capacity. "Clients" are participants other than the session's
# therapist: solo allows 1, couple allows 2, group is unlimited (no entry).
_MODE_CLIENT_CAP = {"solo": 1, "couple": 2}


def _session_is_full(session_id, mode, user_id, therapist_id) -> bool:
    """True if a non-therapist user joining would exceed the mode's client cap.

    The therapist is never counted, and the joining user is excluded from the
    tally — so the therapist, and any client reconnecting/reloading, are always
    allowed back in. Counts only participants currently present.
    """
    cap = _MODE_CLIENT_CAP.get(mode)
    if cap is None or user_id == therapist_id:
        return False
    current_clients = {
        u for u in room_participants.get(session_id, set())
        if u != therapist_id and u != user_id
    }
    return len(current_clients) >= cap


def _claim_display_name(session_id: str, user_id: str, name: str) -> None:
    """Store a display name in both active and taken dicts."""
    session_display_names.setdefault(session_id, {})[user_id] = name
    session_taken_names.setdefault(session_id, set()).add(name.lower())


def _is_name_taken(session_id: str, user_id: str, name: str) -> bool:
    """Return True if name is already taken by *another* user in this session."""
    taken = session_taken_names.get(session_id, set())
    if name.lower() not in taken:
        return False
    # Allow a user to re-confirm their own current name
    current = session_display_names.get(session_id, {}).get(user_id, "")
    return name.lower() != current.lower()


# ---------------------------------------------------------------------------
# Rate limiting — SQLite-backed sliding window
# ---------------------------------------------------------------------------

def _check_rate_limit(user_id: str) -> bool:
    """Return True if within limit, False if exceeded. Persists across restarts."""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    RateLimitEntry.query.filter(
        RateLimitEntry.user_id == user_id,
        RateLimitEntry.timestamp < cutoff,
    ).delete(synchronize_session=False)
    count = RateLimitEntry.query.filter_by(user_id=user_id).count()
    if count >= _RATE_MAX_MSGS:
        db.session.commit()
        return False
    db.session.add(RateLimitEntry(user_id=user_id, timestamp=now))
    db.session.commit()
    return True


def _purge_expired_sessions():
    """Delete sessions (and their messages) whose 30-day retention window has expired.

    Also retires audit log rows older than 6 years (HIPAA § 164.312(b)).
    Called once on startup and then every 24 hours by APScheduler.
    Safe to call in tests — uses app.app_context() internally.
    """
    try:
        with app.app_context():
            now = datetime.now(timezone.utc)
            expired = TherapySession.query.filter(
                TherapySession.retention_expires_at != None,
                TherapySession.retention_expires_at < now,
            ).all()

            # Capture IDs before deletion so we can log after the commit
            purged = [(ts.id, ts.created_by) for ts in expired]

            for ts in expired:
                ChatMessage.query.filter_by(session_id=ts.id).delete(synchronize_session=False)
                Exercise.query.filter_by(user_id=ts.created_by).delete(synchronize_session=False)
                RateLimitEntry.query.filter_by(user_id=ts.created_by).delete(synchronize_session=False)
                db.session.delete(ts)

            if purged:
                db.session.commit()
                app.logger.info("Purged %d expired session(s).", len(purged))
                for sid, uid in purged:
                    log_event("session_purged_auto", session_id=sid, user_id=uid,
                              trigger="scheduler")

            # Retire audit logs older than 6 years (HIPAA minimum retention satisfied)
            six_years_ago = now - timedelta(days=6 * 365)
            old_count = AuditLog.query.filter(AuditLog.timestamp < six_years_ago).delete(
                synchronize_session=False
            )
            if old_count:
                db.session.commit()
                app.logger.info("Retired %d audit log row(s) older than 6 years.", old_count)

    except Exception as exc:
        app.logger.error("Session purge job failed: %s", exc)


if not config.IS_TESTING:
    config.secure_env_file()
    config.validate_config()
    init_encryption(config.FIELD_ENCRYPTION_KEY)
    with app.app_context():
        db.create_all()
        # One-time migration: drop the nickname column — friendly names are now
        # local-only (localStorage) and no longer stored server-side.
        from sqlalchemy import text
        try:
            db.session.execute(text("ALTER TABLE therapy_sessions DROP COLUMN nickname"))
            db.session.commit()
        except Exception:
            db.session.rollback()  # column already removed or never existed
            # rollback (not pass) is required: on Postgres a failed statement
            # aborts the transaction, which would block the migrations below.

        # One-time migration: add the therapist_id column for therapist-led
        # sessions. create_all() does not alter existing tables, so add it here.
        # Idempotent: a no-op once the column exists. Works on SQLite and Postgres.
        try:
            db.session.execute(text("ALTER TABLE therapy_sessions ADD COLUMN therapist_id VARCHAR(36)"))
            db.session.commit()
        except Exception:
            db.session.rollback()  # column already exists

    import threading   # used by the purge-job daemon thread below

    # Add retention_expires_at column if it doesn't exist yet (one-time migration)
    with app.app_context():
        from sqlalchemy import text
        try:
            db.session.execute(text(
                "ALTER TABLE therapy_sessions ADD COLUMN retention_expires_at DATETIME"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()  # column already exists (rollback clears the aborted txn)

    # Add display_name column to chat_messages for participant identification
    with app.app_context():
        from sqlalchemy import text
        try:
            db.session.execute(text(
                "ALTER TABLE chat_messages ADD COLUMN display_name VARCHAR(60)"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()  # column already exists (rollback clears the aborted txn)

    # Remove prompt/response columns from exercises — conversation text is now
    # stored only in chat_messages (encrypted). Metadata-only exercise records
    # are sufficient for the progress chart.
    with app.app_context():
        from sqlalchemy import text, inspect as sa_inspect
        col_names = {c["name"] for c in sa_inspect(db.engine).get_columns("exercises")}
        for col in ("prompt", "response"):
            if col in col_names:
                try:
                    db.session.execute(text(f"ALTER TABLE exercises DROP COLUMN {col}"))
                    db.session.commit()
                except Exception:
                    pass

    # Force-expire legacy sessions whose IDs aren't the current canonical
    # randomized-private-key format (session_id.SESSION_ID_LENGTH). The
    # scheduled purge below will sweep them in the next pass. Idempotent:
    # matching rows already have their retention bumped down, repeat runs
    # are a no-op.
    with app.app_context():
        from sqlalchemy import func
        from session_id import SESSION_ID_LENGTH
        try:
            now_utc = datetime.now(timezone.utc)
            updated = (
                TherapySession.query
                .filter(func.length(TherapySession.id) != SESSION_ID_LENGTH)
                .filter((TherapySession.retention_expires_at == None) | (TherapySession.retention_expires_at > now_utc))
                .update({TherapySession.retention_expires_at: now_utc}, synchronize_session=False)
            )
            db.session.commit()
            if updated:
                app.logger.info(
                    "Force-expired %d legacy session(s) with non-%d-char IDs.",
                    updated, SESSION_ID_LENGTH
                )
        except Exception as exc:
            db.session.rollback()
            app.logger.warning("Legacy session expiration migration skipped: %s", exc)

    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_purge_expired_sessions, "interval", hours=24, id="purge_expired_sessions")
    # Annual ICD code-refresh reminder, emailed to the admin inbox every March 1 (UTC).
    _scheduler.add_job(
        _send_icd_refresh_reminder, "cron",
        month=3, day=1, hour=9, timezone="UTC", id="icd_refresh_reminder",
    )
    _scheduler.start()
    # Run once immediately on startup to catch any sessions that expired while the app was down
    threading.Thread(target=_purge_expired_sessions, daemon=True).start()


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(429)
def ratelimit_handler(e):
    """Return JSON for rate-limit errors so the client can show a clear message."""
    return jsonify({"error": "Too many attempts. Please wait a few minutes and try again."}), 429


# Routes — pages
# ---------------------------------------------------------------------------

_SW_PATH = os.path.join(os.path.dirname(__file__), "static", "js", "sw.js")


@app.route("/sw.js")
def service_worker():
    """Serve the PWA service worker with the Cloud Run revision baked in.

    Substitutes the __BUILD_VERSION__ placeholder in static/js/sw.js with the
    current K_REVISION (set automatically by Cloud Run on every deploy; falls
    back to 'dev' locally). The cache name therefore changes on every deploy,
    causing the new SW's activate handler to delete the prior deploy's cache
    and re-precache fresh assets — no manual cache-version bumps required.
    """
    with open(_SW_PATH, "r", encoding="utf-8") as f:
        body = f.read()
    build_version = os.environ.get("K_REVISION", "dev")
    body = body.replace("__BUILD_VERSION__", build_version)
    response = make_response(body)
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    """Digital Asset Links — required by TWA to verify domain ownership.

    The sha256_cert_fingerprints value below is a placeholder.
    Replace it with the real SHA-256 fingerprint of your Android signing key
    after running: keytool -list -v -keystore <your.keystore>
    """
    import json as _json
    payload = [
        {
            "relation": [
                "delegate_permission/common.handle_all_urls",
                "delegate_permission/common.get_login_creds"
            ],
            "target": {
                "namespace": "android_app",
                "package_name": "com.onlinegbc.togethermindsai",
                "sha256_cert_fingerprints": [
                    "42:CD:CA:85:A9:22:D5:73:2D:CB:1D:57:25:F1:D6:48:C8:09:4C:E2:44:4D:E7:6B:EF:94:B8:BA:0A:3B",
                    "DD:6B:CA:AB:00:41:A0:1A:D4:A2:5F:F5:B5:63:28:AF:B5:06:1F:50:23:54:18:A5:87:E7:1B:D1:84:9F",
                    "DD:6B:CA:AB:00:41:A0:1A:D4:A2:5F:F5:B5:63:28:AF:B5:06:1F:50:23:54:18:A5:87:E7:1B:D1:84:9F:4B:05"
                ]
            }
        }
    ]
    response = make_response(_json.dumps(payload, indent=2))
    response.headers["Content-Type"] = "application/json"
    return response


@app.route("/")
def root():
    # Always land first-touch on the welcome/landing page.
    return redirect(url_for("welcome"))


@app.route("/welcome")
def welcome():
    return render_template("welcome.html")


# ---------------------------------------------------------------------------
# Clinician OAuth login — Google & Microsoft (OpenID Connect)
# ---------------------------------------------------------------------------

def _current_clinician_id():
    """Return the logged-in clinician's account id, or None."""
    return session.get("clinician_id")


def _current_client_account_id():
    """Return the logged-in client's account id, or None."""
    return session.get("client_account_id")


@app.context_processor
def _inject_auth_state():
    """Expose login state to every template (drives the navbar)."""
    return {
        "current_clinician_id": session.get("clinician_id"),
        "current_client_account_id": session.get("client_account_id"),
        "current_year": datetime.now(timezone.utc).year,
    }


@app.route("/login")
def login():
    """Clinician login page — Sign in with Google / Microsoft."""
    if _current_clinician_id():
        return redirect(url_for("therapist_start"))
    return render_template("login.html")


# ---------------------------------------------------------------------------
# Shared OAuth plumbing — used by BOTH the clinician routes (/auth/...) and the
# client routes (/client/auth/...). The network/OIDC exchange lives here once;
# each role's route owns only "which account to create and where to send them".
# ---------------------------------------------------------------------------

def _oauth_start(provider, callback_endpoint):
    """Redirect the user to the provider's consent screen, returning to
    `callback_endpoint`. Returns a Flask response (redirect or error)."""
    if provider not in _OAUTH_PROVIDERS:
        return _redirect_invalid_session()
    client = oauth.create_client(provider)
    redirect_uri = url_for(callback_endpoint, provider=provider, _external=True, _scheme="https")
    return client.authorize_redirect(redirect_uri)


def _oauth_subject(provider):
    """Complete the OAuth exchange and return the provider's stable subject id,
    or None on failure (the caller decides where to redirect). Never raises."""
    client = oauth.create_client(provider)
    # Microsoft multi-tenant returns a tenant-substituted issuer that fails
    # Authlib's default issuer check — validate it the way Microsoft documents.
    kwargs = {}
    if provider == "microsoft":
        kwargs["claims_options"] = {"iss": {"essential": True, "validate": _validate_ms_issuer}}
    try:
        token = client.authorize_access_token(**kwargs)
    except Exception as exc:
        app.logger.error("OAuth callback failed (%s): %s: %s", provider, type(exc).__name__, exc)
        return None
    userinfo = token.get("userinfo") or {}
    return userinfo.get("sub")


def _safe_next(target):
    """Return `target` only if it's a safe same-site relative path, else None.
    Guards the post-login redirect against open-redirect abuse."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


# ---------------------------------------------------------------------------
# Clinician OAuth routes
# ---------------------------------------------------------------------------

@app.route("/auth/<provider>/login")
def oauth_login(provider):
    return _oauth_start(provider, "oauth_callback")


@app.route("/auth/<provider>/callback")
def oauth_callback(provider):
    if provider not in _OAUTH_PROVIDERS:
        return _redirect_invalid_session()
    subject = _oauth_subject(provider)
    if not subject:
        flash("Sign-in did not complete. Please try again.", "warning")
        return redirect(url_for("login"))

    now = datetime.now(timezone.utc)
    clinician = (
        Clinician.query
        .filter_by(provider=provider, provider_subject=subject)
        .first()
    )
    if clinician is None:
        clinician = Clinician(
            id=str(uuid.uuid4()), provider=provider, provider_subject=subject,
            created_at=now, last_login_at=now,
        )
        db.session.add(clinician)
        log_event("clinician_registered", user_id=clinician.id, provider=provider)
    else:
        clinician.last_login_at = now
    db.session.commit()

    # The clinician's account id is their identity everywhere (session owner).
    session["user_id"]      = clinician.id
    session["clinician_id"] = clinician.id
    session.permanent = True
    log_event("clinician_login", user_id=clinician.id, provider=provider)
    return redirect(url_for("therapist_start"))


@app.route("/logout")
def logout():
    cid = _current_clinician_id()
    if cid:
        log_event("clinician_logout", user_id=cid)
    client_id = _current_client_account_id()
    if client_id:
        log_event("client_logout", user_id=client_id)
    session.clear()
    return redirect(url_for("welcome"))


# ---------------------------------------------------------------------------
# Client OAuth routes — optional login so a client can find their past sessions
# across devices. Separate from the clinician routes so a client sign-in can
# never create a Clinician account; both share the _oauth_* helpers above.
# ---------------------------------------------------------------------------

@app.route("/client/login")
def client_login():
    """Optional client sign-in page — Google / Microsoft."""
    if _current_client_account_id():
        return redirect(url_for("my_sessions"))
    # Optionally remember where to return after login (e.g. a session URL).
    nxt = _safe_next(request.args.get("next"))
    if nxt:
        session["client_login_next"] = nxt
    return render_template("client_login.html")


@app.route("/client/auth/<provider>/login")
def client_oauth_login(provider):
    return _oauth_start(provider, "client_oauth_callback")


@app.route("/client/auth/<provider>/callback")
def client_oauth_callback(provider):
    if provider not in _OAUTH_PROVIDERS:
        return _redirect_invalid_session()
    subject = _oauth_subject(provider)
    if not subject:
        flash("Sign-in did not complete. Please try again.", "warning")
        return redirect(url_for("client_login"))

    now = datetime.now(timezone.utc)
    account = (
        ClientAccount.query
        .filter_by(provider=provider, provider_subject=subject)
        .first()
    )
    if account is None:
        account = ClientAccount(
            id=str(uuid.uuid4()), provider=provider, provider_subject=subject,
            created_at=now, last_login_at=now,
        )
        db.session.add(account)
        log_event("client_registered", user_id=account.id, provider=provider)
    else:
        account.last_login_at = now
    db.session.commit()

    # The account id becomes the client's stable user_id, so their messages link
    # across devices and "my sessions" can find the sessions they took part in.
    session["user_id"]           = account.id
    session["client_account_id"] = account.id
    session.permanent = True
    log_event("client_login", user_id=account.id, provider=provider)

    nxt = _safe_next(session.pop("client_login_next", None))
    return redirect(nxt or url_for("my_sessions"))


@app.route("/me/sessions")
def my_sessions():
    """A logged-in client's list of therapist-led sessions they took part in."""
    account_id = _current_client_account_id()
    if not account_id:
        return redirect(url_for("client_login"))
    # Sessions the client took part in: recorded at join time (covers silent
    # attendees) unioned with any session they sent a message in (defensive —
    # covers rows that predate join tracking).
    session_ids = {
        row[0] for row in
        db.session.query(SessionParticipant.session_id).filter_by(user_id=account_id).all()
    } | {
        row[0] for row in
        db.session.query(ChatMessage.session_id).filter_by(user_id=account_id).distinct().all()
    }
    sessions = []
    if session_ids:
        sessions = (
            TherapySession.query
            .filter(TherapySession.id.in_(session_ids))
            .order_by(TherapySession.created_at.desc())
            .all()
        )
    return render_template("my_sessions.html", my_sessions=sessions)


@app.route("/therapist")
def therapist_start():
    """Landing page for a logged-in clinician to start / resume therapist-led sessions."""
    clinician_id = _current_clinician_id()
    if not clinician_id:
        return redirect(url_for("login"))
    my_sessions = (
        TherapySession.query
        .filter_by(therapist_id=clinician_id)
        .order_by(TherapySession.created_at.desc())
        .limit(20)
        .all()
    )
    return render_template("therapist.html", my_sessions=my_sessions)


@app.route("/therapist/start/<mode>", methods=["POST"])
def therapist_start_session(mode):
    """Create a new therapist-led session owned by the logged-in clinician."""
    clinician_id = _current_clinician_id()
    if not clinician_id:
        return redirect(url_for("login"))
    if mode not in ("solo", "couple", "group"):
        return _redirect_invalid_session()

    now = datetime.now(timezone.utc)
    new_sid = generate_session_id()
    db.session.add(TherapySession(
        id=new_sid, mode=mode, created_by=clinician_id,
        created_at=now,
        # Clinical record retention (~6 years), not the 30-day consumer purge.
        retention_expires_at=now + _CLINICAL_RETENTION_DELTA,
        therapist_id=clinician_id,
    ))
    db.session.commit()
    log_event("session_created", session_id=new_sid, user_id=clinician_id,
              mode=mode, therapist_led=True)
    return redirect(url_for(f"therapy_{mode}", session_id=new_sid))


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/tos")
def tos():
    return render_template("tos.html")


@app.route("/auth/<therapy_mode>", methods=["GET"])
def auth_get(therapy_mode):
    # This page only exists to take a client into a clinician-led session they are
    # joining. Without a pending join there is nothing self-directed to start here.
    if not (session.get("pending_solo_session")
            or session.get("pending_couple_session")
            or session.get("pending_group_session")):
        return redirect(url_for("session_join_get"))
    return render_template("auth.html", therapy_mode=therapy_mode)


@app.route("/auth/<therapy_mode>", methods=["POST"])
@limiter.limit("10 per hour")
def auth_post(therapy_mode):
    """Legacy form-based auth fallback. Only JOINS an existing clinician-led session
    (stashed by /session/join); it never creates a new self-directed session."""
    pending_couple = session.pop("pending_couple_session", None)
    pending_group  = session.pop("pending_group_session",  None)
    pending_solo   = session.pop("pending_solo_session",   None)
    if not (pending_solo or pending_couple or pending_group):
        return redirect(url_for("session_join_get"))

    user_id = str(uuid.uuid4())
    db.session.add(User(id=user_id, therapy_mode=therapy_mode))
    db.session.commit()
    session["user_id"] = user_id

    if pending_solo:
        return redirect(url_for("therapy_solo", session_id=pending_solo))
    elif pending_couple:
        return redirect(url_for("therapy_couple", session_id=pending_couple))
    else:
        return redirect(url_for("therapy_group", session_id=pending_group))


def _session_exists(session_id: str) -> bool:
    """True iff a TherapySession row with that id exists. Used as the entry
    gate for all /therapy/<mode>/<session_id> routes — without this check,
    the routes would accept any string and silently create orphan rows."""
    if not session_id:
        return False
    return db.session.get(TherapySession, session_id) is not None


def _redirect_invalid_session():
    """Redirect to the welcome/landing page with a flash explaining the
    session is unknown/expired."""
    flash("That session doesn't exist or has expired.", "warning")
    return redirect(url_for("welcome"))


def _render_session_room(session_id, mode):
    """Render the shared realtime room for any mode (solo / couple / group).

    The three legacy routes are thin wrappers over this. `ts.mode` is the
    authoritative mode — a URL pointing at the wrong one is redirected to the
    canonical room, so a couple session is always shown as a couple.
    """
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode=mode))
    if not _session_exists(session_id):
        return _redirect_invalid_session()
    ts = db.session.get(TherapySession, session_id)

    # The stored mode wins over the URL — keeps every mode honestly labelled.
    if ts and ts.mode in MODE_CONFIG and ts.mode != mode:
        return redirect(url_for(f"therapy_{ts.mode}", session_id=session_id))

    # Solo is therapist-led only on this branch; a solo room without a therapist
    # is a stale/invalid record (the consumer 1:1 flow was removed).
    if mode == "solo" and not (ts and ts.therapist_id):
        return _redirect_invalid_session()

    # Enforce the per-mode client capacity before admitting a client into the room.
    if _session_is_full(session_id, mode, user_id, ts.therapist_id if ts else None):
        flash("That session is already full.", "warning")
        return redirect(url_for("welcome"))

    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["solo"])
    return render_template(
        "session_live.html",
        user_id=user_id, session_id=session_id, mode=mode,
        mode_label=cfg["label"], mode_icon=cfg["icon"], mode_placeholder=cfg["placeholder"],
        is_therapist=bool(ts and ts.therapist_id and ts.therapist_id == user_id),
        is_therapist_led=bool(ts and ts.therapist_id),
        rtc_enabled=config.RTC_ENABLED,
    )


@app.route("/therapy/solo/<session_id>", methods=["GET"])
def therapy_solo(session_id):
    return _render_session_room(session_id, "solo")


@app.route("/therapy/couple/<session_id>")
def therapy_couple(session_id):
    return _render_session_room(session_id, "couple")


@app.route("/therapy/group/<session_id>")
def therapy_group(session_id):
    return _render_session_room(session_id, "group")


# ---------------------------------------------------------------------------
# Realtime conferencing (Phase 1) — short-lived tokens minted server-side so the
# LiveKit secret and AssemblyAI key never reach the browser. A caller must have a
# session identity and be pointing at a real session.
# ---------------------------------------------------------------------------

def _rtc_guard(session_id):
    """Return (user_id, error_response). error_response is None when allowed."""
    if not config.RTC_ENABLED:
        return None, (jsonify({"error": "rtc_disabled"}), 503)
    user_id = session.get("user_id")
    if not user_id:
        return None, (jsonify({"error": "no_identity"}), 403)
    if not _session_exists(session_id):
        return None, (jsonify({"error": "no_session"}), 404)
    return user_id, None


@app.route("/rtc/livekit-token", methods=["POST"])
@limiter.limit("30 per minute")
def rtc_livekit_token():
    """Mint a LiveKit join token for the caller to join the session's audio room."""
    session_id = (request.get_json(silent=True) or {}).get("session_id", "")
    user_id, err = _rtc_guard(session_id)
    if err:
        return err
    from livekit.api import AccessToken, VideoGrants
    display = session_display_names.get(session_id, {}).get(user_id) or "Participant"
    token = (
        AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity(user_id)
        .with_name(display)
        .with_grants(VideoGrants(
            room_join=True, room=session_id,
            can_publish=True, can_subscribe=True,
        ))
        .to_jwt()
    )
    return jsonify({"token": token, "url": config.LIVEKIT_URL})


@app.route("/rtc/stt-token", methods=["POST"])
@limiter.limit("30 per minute")
def rtc_stt_token():
    """Mint a short-lived AssemblyAI streaming token for in-browser transcription."""
    session_id = (request.get_json(silent=True) or {}).get("session_id", "")
    user_id, err = _rtc_guard(session_id)
    if err:
        return err
    import requests
    try:
        resp = requests.get(
            "https://streaming.assemblyai.com/v3/token",
            headers={"Authorization": config.ASSEMBLYAI_API_KEY},
            params={"expires_in_seconds": 600},
            timeout=10,
        )
        resp.raise_for_status()
        return jsonify({"token": resp.json().get("token")})
    except Exception as exc:
        app.logger.error("AssemblyAI token error: %s", type(exc).__name__)
        return jsonify({"error": "stt_token_failed"}), 502


@app.route("/api/display-name", methods=["POST"])
def api_set_display_name():
    """AJAX endpoint used by solo mode to set or rename a display name.

    Couple/group modes use the set_display_name / rename socket events instead.
    Request JSON: {session_id, display_name}
    Response JSON: {display_name} on success, {error} on conflict/validation error.
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    name = data.get("display_name", "").strip()

    if not name:
        return jsonify({"error": "Display name cannot be empty."}), 400
    if len(name) > 40:
        return jsonify({"error": "Display name must be 40 characters or fewer."}), 400

    if _is_name_taken(session_id, user_id, name):
        return jsonify({"error": f"'{name}' is already taken in this session. Please choose another."}), 409

    _claim_display_name(session_id, user_id, name)
    return jsonify({"display_name": name}), 200


@app.route("/api/translate-check", methods=["POST"])
@limiter.limit("120 per hour")
def api_translate_check():
    """Detect language; if non-English, translate to English.

    Returns:
      {"is_english": true} — text is English; client sends as-is
      {"is_english": false, "translation": "<English>"} — show user the
        translation and have them confirm before the message proceeds

    On any error: returns is_english=true so the user is never blocked.
    Crisis-signal preservation matters: uses Sonnet 4.6 with a prompt that
    explicitly preserves emotional intensity and clinical idioms (e.g.,
    "I want to disappear" must keep its suicidal-ideation connotation in the
    English output, not flatten to "I want to leave").
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400
    if len(text) > config.MAX_MESSAGE_LENGTH:
        return jsonify({"error": "Text too long"}), 413

    try:
        from ai_therapist import _get_claude_client
        client = _get_claude_client()
        result = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    "Detect the language of the message below.\n"
                    "If it is in English, reply with EXACTLY the two letters: EN\n"
                    "Otherwise, reply with ONLY the English translation. "
                    "Preserve emotional intensity and any culturally specific idioms "
                    "in their clinical sense (for example, '消えたい' or "
                    "'me quiero morir' must convey suicidal ideation, not be flattened "
                    "to 'I want to leave'). Do not add explanations, prefixes, or quotes.\n\n"
                    "Message:\n" + text
                )
            }]
        )
        reply = (result.content[0].text or "").strip()
        if reply.upper() == "EN":
            return jsonify({"is_english": True}), 200
        return jsonify({"is_english": False, "translation": reply}), 200
    except Exception as exc:
        app.logger.error("translate-check failed for user %s: %s", user_id, exc)
        # Fail open — never block the user from sending due to a translate error.
        return jsonify({"is_english": True, "fallback": True}), 200


# ---------------------------------------------------------------------------
# Feedback form — email-only delivery, no DB storage, no audit log entry,
# no IP / user_id / PII captured. Per FEEDBACK_FORM_PLAN.md.
# ---------------------------------------------------------------------------

_FEEDBACK_TEXT_MAX = 1000
_FEEDBACK_PLATFORMS = {"web", "android_twa", "ios_pwa", "mobile_browser"}
_FEEDBACK_MODES = {"solo", "couple", "group", None}
_FEEDBACK_PAY = {"yes", "maybe", "no", None}
_FEEDBACK_RATINGS = {1, 2, 3, 4, 5, None}
_FEEDBACK_OS = {"windows", "macos", "linux", "android", "ios", "unknown", None}

# In-memory cooldown keyed by Flask session cookie (NOT IP).
# Resets on process restart — acceptable for an anti-spam guard.
_feedback_last_submit: dict = {}   # session_key -> last unix epoch
_feedback_daily_count: dict = {}   # (session_key, utc_date_iso) -> count

_FEEDBACK_COOLDOWN_SECONDS = 60
_FEEDBACK_DAILY_MAX = 10


def _feedback_session_key() -> str:
    """Stable per-session key used for rate limiting. Creates a server-side
    token in the Flask session if one is missing — no IP, no user_id."""
    key = session.get("_fb_key")
    if not key:
        key = secrets.token_urlsafe(16)
        session["_fb_key"] = key
    return key


def _device_label(platform: str, os_name) -> str:
    """Friendly human-readable device descriptor combining shell + OS."""
    if platform == "android_twa":
        return "Android (installed app)"
    if platform == "ios_pwa":
        return "iPhone/iPad (installed app)"
    if platform == "mobile_browser":
        if os_name == "android":
            return "Android (mobile browser)"
        if os_name == "ios":
            return "iPhone/iPad (mobile browser)"
        return "Mobile browser"
    # platform == "web"
    if os_name == "windows":
        return "Windows laptop / desktop"
    if os_name == "macos":
        return "Mac laptop / desktop"
    if os_name == "linux":
        return "Linux laptop / desktop"
    return "Laptop / desktop"


def _mode_label(mode) -> str:
    return {
        "solo": "1:1 Session",
        "couple": "Couple Check-in",
        "group": "Group Circle",
    }.get(mode, "Not in a session")


def _pay_label(pay) -> str:
    return {"yes": "Yes", "maybe": "Maybe", "no": "No"}.get(pay, "Not answered")


def _stars(rating) -> str:
    if not rating:
        return "Not rated"
    filled = "★" * int(rating)
    empty = "☆" * (5 - int(rating))
    return f"{filled}{empty}"


def _format_feedback_email(payload: dict) -> tuple:
    """Build (subject, plain_body, html_body) for the feedback email."""
    rating = payload.get("rating")
    rating_str = f"{rating} / 5" if rating else "N/A"
    pay = payload.get("would_pay")
    platform = payload.get("platform") or ""
    os_name = payload.get("os")
    mode = payload.get("mode")
    device = _device_label(platform, os_name)
    mode_label = _mode_label(mode)
    pay_label = _pay_label(pay)
    stars = _stars(rating)
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    subject = f"TogetherMindsAI feedback — {mode_label} on {device} — {rating_str}"

    # ----- Plain-text body (fallback) ---------------------------------------
    def text_section(title: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return f"{title}:\n  (none)\n"
        return f"{title}:\n{text}\n"

    plain = (
        f"Rating:           {rating_str}\n"
        f"Would pay:        {pay_label}\n"
        f"Device:           {device}\n"
        f"Session mode:     {mode_label}\n"
        f"Submitted at:     {submitted_at}\n"
        f"\n"
        + text_section("What worked well", payload.get("what_worked", ""))
        + "\n"
        + text_section("What could be improved", payload.get("what_to_improve", ""))
        + "\n"
        + text_section("Desired features", payload.get("desired_features", ""))
        + "\n"
        + text_section("Anything else", payload.get("other", ""))
    )

    # ----- HTML body --------------------------------------------------------
    from html import escape as h

    def html_card(title: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return (
                f'<h3 style="color:#388E3C;font-size:15px;margin:24px 0 8px;font-weight:600;">{h(title)}</h3>'
                f'<div style="padding:14px 16px;background:#f7faf7;border-left:3px solid #d0d0d0;border-radius:4px;color:#999;font-style:italic;">(none)</div>'
            )
        return (
            f'<h3 style="color:#388E3C;font-size:15px;margin:24px 0 8px;font-weight:600;">{h(title)}</h3>'
            f'<div style="padding:14px 16px;background:#f7faf7;border-left:3px solid #4CAF50;border-radius:4px;white-space:pre-wrap;line-height:1.5;">{h(text)}</div>'
        )

    rating_html = (
        f'<span style="color:#FFC107;font-size:18px;letter-spacing:2px;">{stars}</span> '
        f'<span style="color:#999;margin-left:6px;">({h(rating_str)})</span>'
    ) if rating else f'<span style="color:#999;font-style:italic;">{h(stars)}</span>'

    meta_row = (
        '<tr>'
        '<td style="padding:14px 16px;border-bottom:1px solid #e6efe6;width:160px;font-weight:600;color:#555;">{label}</td>'
        '<td style="padding:14px 16px;border-bottom:1px solid #e6efe6;color:#212121;">{value}</td>'
        '</tr>'
    )
    meta_row_last = (
        '<tr>'
        '<td style="padding:14px 16px;width:160px;font-weight:600;color:#555;">{label}</td>'
        '<td style="padding:14px 16px;color:#212121;">{value}</td>'
        '</tr>'
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f7f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#212121;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7f5;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,0.06);">
  <tr><td style="background:#4CAF50;padding:22px 28px;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.9;">TogetherMindsAI</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px;">New feedback received</div>
  </td></tr>

  <tr><td style="padding:24px 28px 8px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7faf7;border-radius:8px;border:1px solid #e6efe6;border-collapse:separate;border-spacing:0;">
      {meta_row.format(label='Rating', value=rating_html)}
      {meta_row.format(label='Would pay', value=h(pay_label))}
      {meta_row.format(label='Device', value=h(device))}
      {meta_row.format(label='Session mode', value=h(mode_label))}
      {meta_row_last.format(label='Submitted', value=h(submitted_at))}
    </table>
  </td></tr>

  <tr><td style="padding:0 28px 24px;">
    {html_card('What worked well', payload.get('what_worked', ''))}
    {html_card('What could be improved', payload.get('what_to_improve', ''))}
    {html_card('Desired features', payload.get('desired_features', ''))}
    {html_card('Anything else', payload.get('other', ''))}
  </td></tr>

  <tr><td style="padding:14px 24px;background:#fafafa;color:#999;font-size:11px;text-align:center;border-top:1px solid #eee;">
    No IP, no name, no session content captured.
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    return subject, plain, html_body


def _send_feedback_email(subject: str, plain_body: str, html_body: str) -> None:
    """Send feedback via Gmail SMTP as multipart/alternative (text + HTML).
    Raises on failure — caller maps to 503."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.FEEDBACK_FROM_EMAIL or config.FEEDBACK_SMTP_USER
    msg["To"] = ", ".join(config.FEEDBACK_TO_EMAILS)
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(config.FEEDBACK_SMTP_HOST, config.FEEDBACK_SMTP_PORT, timeout=5) as smtp:
        smtp.starttls()
        smtp.login(config.FEEDBACK_SMTP_USER, config.FEEDBACK_SMTP_PASSWORD)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Annual ICD code-refresh reminder — emailed from the app every March 1.
# WHO ships ~annual ICD-11 releases; this nudges a refresh of the therapist
# co-pilot's ICD grounding (clinical_data/icd_corpus.json) and carries the full
# runbook so the steps don't have to be remembered. Reuses the feedback SMTP
# path and admin inbox (config.FEEDBACK_TO_EMAILS).
# ---------------------------------------------------------------------------

def _icd_refresh_reminder_content():
    """Return (subject, plain_body, html_body) for the annual ICD-refresh email."""
    subject = "TogetherMindsAI — Annual ICD code refresh due (co-pilot grounding)"

    steps = [
        ("Why", "WHO publishes ICD-11 on a roughly annual release cycle. Refresh the therapist "
                "co-pilot's ICD grounding so the ICD-11 deep links point at the current release."),
        ("1. Credentials", "The WHO ICD-API is free (registration only). Client credentials are "
                "WHO_ICD_ClientId / WHO_ICD_ClientSecret, stored in the local .env (gitignored) AND "
                "in Google Secret Manager. Make sure .env has them before running the script."),
        ("2. Harvest (dry run)", "Run: python scripts/harvest_icd11_entities.py — this authenticates "
                "to the ICD-API, prints the latest MMS release, and resolves each ICD-11 code in "
                "clinical_data/icd_corpus.json to its WHO Foundation entity id + deep-link URL."),
        ("3. Apply", "Re-run with --write: python scripts/harvest_icd11_entities.py --write — this "
                "writes the refreshed icd11_url deep links back into clinical_data/icd_corpus.json."),
        ("4. Verify", "Spot-check a few resolved entity titles match the corpus labels, then run: "
                "pytest tests/test_clinical_reference.py tests/test_copilot.py"),
        ("5. Commit", "Commit & push the updated clinical_data/icd_corpus.json (never commit .env)."),
        ("ICD-10 note", "ICD-10 links are search-by-code and need no refresh — they don't break on a "
                "new release. The ICD-11 deep links are the thing this reminder is about."),
    ]

    plain = (
        "Annual ICD code refresh for the therapist co-pilot.\n\n"
        + "\n\n".join(f"{label}:\n  {text}" for label, text in steps)
        + "\n\nSee project memory: project_who_icd_api.\n"
    )

    from html import escape as h
    rows = "".join(
        f'<h3 style="color:#388E3C;font-size:15px;margin:18px 0 4px;">{h(label)}</h3>'
        f'<div style="line-height:1.5;color:#212121;">{h(text)}</div>'
        for label, text in steps
    )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;">'
        '<h2 style="color:#2e7d32;">Annual ICD code refresh due</h2>'
        '<p style="color:#555;">A yearly nudge to refresh the therapist co-pilot\'s ICD grounding.</p>'
        f"{rows}"
        '<p style="color:#888;font-size:12px;margin-top:20px;">Sent automatically by TogetherMindsAI on March 1.</p>'
        "</div>"
    )
    return subject, plain, html_body


def _send_icd_refresh_reminder():
    """Email the annual ICD-refresh runbook to the admin inbox. Never raises."""
    if not (config.FEEDBACK_SMTP_USER and config.FEEDBACK_SMTP_PASSWORD):
        app.logger.warning("ICD refresh reminder skipped — SMTP creds not configured.")
        return
    try:
        subject, plain, html_body = _icd_refresh_reminder_content()
        _send_feedback_email(subject, plain, html_body)
        app.logger.info("ICD refresh reminder email sent.")
    except Exception:
        # Don't log the body — it could echo SMTP server detail.
        app.logger.warning("ICD refresh reminder email failed to send.")


@app.route("/feedback")
def feedback_page():
    """Standalone feedback form. Public — no login required."""
    return render_template("feedback.html")


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """Receive feedback, send via email. Stateless — no DB, no audit, no IP."""
    payload = request.get_json(silent=True) or {}

    # --- Validation ---------------------------------------------------------
    rating = payload.get("rating")
    if rating is not None:
        # bool is a subclass of int — reject explicitly. Reject floats and
        # strings outright so the contract is "rating is int|null".
        if isinstance(rating, bool) or not isinstance(rating, int):
            return jsonify({"error": "Invalid rating"}), 400
    if rating not in _FEEDBACK_RATINGS:
        return jsonify({"error": "Invalid rating"}), 400

    would_pay = payload.get("would_pay")
    if would_pay == "":
        would_pay = None
    if would_pay is not None and not isinstance(would_pay, str):
        return jsonify({"error": "Invalid would_pay"}), 400
    if would_pay not in _FEEDBACK_PAY:
        return jsonify({"error": "Invalid would_pay"}), 400

    platform = payload.get("platform")
    if not isinstance(platform, str) or platform not in _FEEDBACK_PLATFORMS:
        return jsonify({"error": "Invalid platform"}), 400

    os_name = payload.get("os")
    if os_name == "":
        os_name = None
    if os_name is not None and not isinstance(os_name, str):
        return jsonify({"error": "Invalid os"}), 400
    if os_name not in _FEEDBACK_OS:
        return jsonify({"error": "Invalid os"}), 400

    mode = payload.get("mode")
    if mode == "":
        mode = None
    if mode is not None and not isinstance(mode, str):
        return jsonify({"error": "Invalid mode"}), 400
    if mode not in _FEEDBACK_MODES:
        return jsonify({"error": "Invalid mode"}), 400

    text_fields = ("what_worked", "what_to_improve", "desired_features", "other")
    cleaned = {}
    for f in text_fields:
        val = payload.get(f, "") or ""
        if not isinstance(val, str):
            return jsonify({"error": f"Invalid {f}"}), 400
        if len(val) > _FEEDBACK_TEXT_MAX:
            return jsonify({"error": f"{f} too long (max {_FEEDBACK_TEXT_MAX} chars)"}), 400
        cleaned[f] = val.strip()

    has_content = (
        rating is not None
        or would_pay is not None
        or any(cleaned[f] for f in text_fields)
    )
    if not has_content:
        return jsonify({"error": "Empty submission"}), 400

    # --- Rate limiting (cookie-based, no IP) --------------------------------
    key = _feedback_session_key()
    now = time.time()
    today_iso = datetime.now(timezone.utc).date().isoformat()

    last = _feedback_last_submit.get(key, 0)
    if now - last < _FEEDBACK_COOLDOWN_SECONDS:
        return jsonify({"error": "Please wait a moment before submitting again."}), 429

    daily_key = (key, today_iso)
    # Drop stale daily entries (different date)
    stale_keys = [k for k in _feedback_daily_count if k[0] == key and k[1] != today_iso]
    for k in stale_keys:
        _feedback_daily_count.pop(k, None)
    if _feedback_daily_count.get(daily_key, 0) >= _FEEDBACK_DAILY_MAX:
        return jsonify({"error": "Daily feedback limit reached."}), 429

    # --- Build & send -------------------------------------------------------
    subject, plain_body, html_body = _format_feedback_email({
        "rating": rating,
        "would_pay": would_pay,
        "platform": platform,
        "os": os_name,
        "mode": mode,
        **cleaned,
    })

    if not (config.FEEDBACK_SMTP_USER and config.FEEDBACK_SMTP_PASSWORD):
        return jsonify({"error": "Feedback service not configured"}), 503

    try:
        _send_feedback_email(subject, plain_body, html_body)
    except Exception:
        # Intentionally do not log the exception body — it could echo SMTP
        # auth or the email content. A generic warning suffices.
        app.logger.warning("Feedback SMTP send failed")
        return jsonify({"error": "Could not send feedback right now. Please try again later."}), 503

    _feedback_last_submit[key] = now
    _feedback_daily_count[daily_key] = _feedback_daily_count.get(daily_key, 0) + 1
    return jsonify({"ok": True}), 200


@app.route("/progress/<user_id>/<therapy_mode>")
def progress(user_id, therapy_mode):
    exercises = (
        Exercise.query
        .filter_by(user_id=user_id)
        .order_by(Exercise.timestamp.asc())
        .all()
    )

    week_counts = {}
    for ex in exercises:
        iso = ex.timestamp.isocalendar()
        week_label = f"{iso[0]}-W{iso[1]:02d}"
        week_counts[week_label] = week_counts.get(week_label, 0) + 1

    chart_data = [{"week": w, "count": c} for w, c in sorted(week_counts.items())]

    # Find the user's most recent session (the session_id of their latest
    # message). Used for the transcript download links — without this the
    # template would have to guess which session to download.
    latest_session_id = (
        db.session.query(ChatMessage.session_id)
        .filter_by(user_id=user_id)
        .order_by(ChatMessage.timestamp.desc())
        .limit(1)
        .scalar()
    )

    return render_template(
        "progress.html",
        chart_data=chart_data,
        user_id=user_id,
        therapy_mode=therapy_mode,
        session_id=latest_session_id,
    )


# ---------------------------------------------------------------------------
# Routes — session resumption
# ---------------------------------------------------------------------------

def _join_template(**kwargs):
    """Render join_session.html with format hint context always populated."""
    return render_template(
        "join_session.html",
        rejoin_hint=rejoin_format_hint(),
        rejoin_placeholder=rejoin_placeholder(),
        **kwargs,
    )


@app.route("/session/join", methods=["GET"])
def session_join_get():
    return _join_template()


@app.route("/session/join", methods=["POST"])
def session_join_post():
    raw = request.form.get("session_id", "").strip()
    if not raw:
        return _join_template(error="Please enter a Session ID.")

    # Case-insensitive lookup: normalize both sides to uppercase
    from sqlalchemy import func as sa_func
    ts = TherapySession.query.filter(
        sa_func.upper(TherapySession.id) == raw.upper()
    ).first()
    if not ts:
        return _join_template(error="Session not found. Check the ID and try again.")

    session_id = ts.id  # always use the real ID from DB

    if ts.mode == "solo":
        # Solo sessions are therapist-led only on this branch; a solo row without a
        # therapist_id is a stale/invalid record and is treated as not found.
        if not ts.therapist_id:
            return _join_template(error="Session not found. Check the ID and try again.")
        # Therapist-led 1:1 — the joiner is a CLIENT and must get their OWN identity
        # (never the therapist's). Mirror the couple/group join flow.
        user_id = session.get("user_id")
        if not user_id:
            session["pending_solo_session"] = session_id
            return redirect(url_for("auth_get", therapy_mode="solo"))
        return redirect(url_for("therapy_solo", session_id=session_id))

    user_id = session.get("user_id")
    if not user_id:
        # Stash the real session_id so auth_post can redirect back to the right room
        if ts.mode == "couple":
            session["pending_couple_session"] = session_id
        else:
            session["pending_group_session"] = session_id
        return redirect(url_for("auth_get", therapy_mode=ts.mode))

    if ts.mode == "couple":
        return redirect(url_for("therapy_couple", session_id=session_id))
    else:
        return redirect(url_for("therapy_group", session_id=session_id))




# ---------------------------------------------------------------------------
# Routes — GDPR data deletion
# ---------------------------------------------------------------------------

@app.route("/user/<user_id>", methods=["DELETE"])
def delete_user(user_id):
    if session.get("user_id") != user_id:
        return jsonify({"error": "Forbidden"}), 403

    Exercise.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ChatMessage.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    RateLimitEntry.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    User.query.filter_by(id=user_id).delete(synchronize_session=False)
    db.session.commit()
    log_event("data_deleted_user", user_id=user_id, trigger="user_gdpr_request")

    session.clear()
    return jsonify({"deleted": True}), 200


# ---------------------------------------------------------------------------
# Routes — transcript download
# ---------------------------------------------------------------------------

def _transcript_data(session_id):
    """Return (messages, mode, generated_at) for a given session_id."""
    messages = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    ts = db.session.get(TherapySession, session_id)
    user = db.session.get(User, ts.created_by) if ts else None
    mode = user.therapy_mode.capitalize() if user else "Unknown"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return messages, mode, generated_at


_FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_FONT_REGULAR = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
_FONT_BOLD    = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")


def _user_can_access_session(session_id: str, user_id: str) -> bool:
    """A user can download a session's transcript if they created it OR
    posted any message in it. Covers solo (creator-only) and couple/group
    (creator + every participant)."""
    if not user_id:
        return False
    ts = db.session.get(TherapySession, session_id)
    if not ts:
        return False
    if ts.created_by == user_id:
        return True
    # The clinician who led the session can always retrieve its full record.
    if ts.therapist_id and ts.therapist_id == user_id:
        return True
    return ChatMessage.query.filter_by(session_id=session_id, user_id=user_id).first() is not None


@app.route("/transcript/<session_id>/pdf")
def download_transcript_pdf(session_id):
    if not _user_can_access_session(session_id, session.get("user_id")):
        return jsonify({"error": "Forbidden"}), 403

    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    messages, mode, generated_at = _transcript_data(session_id)

    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_font("DejaVu",      fname=_FONT_REGULAR)
    pdf.add_font("DejaVu", "B", fname=_FONT_BOLD)
    pdf.add_page()

    # Title
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 10, "TogetherMindsAI \u2014 Session Transcript",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Metadata
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Session ID : {session_id}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Mode       : {mode}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, f"Generated  : {generated_at}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # Divider
    pdf.set_draw_color(200, 200, 200)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(6)

    # Assign a distinct RGB color to each human participant
    _PDF_PARTICIPANT_COLORS = [
        (30,  80,  160),   # blue
        (146, 39,  15),    # rust red
        (107, 33,  168),   # purple
        (13,  110, 110),   # teal
        (180, 90,  9),     # amber
        (26,  92,  46),    # dark green
    ]
    pdf_participant_color: dict = {}
    _pdf_color_idx = 0
    for msg in messages:
        if msg.user_id != "AI" and msg.user_id not in pdf_participant_color:
            pdf_participant_color[msg.user_id] = _PDF_PARTICIPANT_COLORS[_pdf_color_idx % len(_PDF_PARTICIPANT_COLORS)]
            _pdf_color_idx += 1

    if not messages:
        pdf.set_text_color(120, 120, 120)
        pdf.set_font("DejaVu", "", 11)
        pdf.cell(0, 8, "No messages recorded for this session.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            if is_ai:
                speaker = "AI Co-Pilot"
            elif msg.display_name:
                speaker = f"{session_id}-{msg.display_name}"
            else:
                speaker = "User"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker + timestamp line
            pdf.set_font("DejaVu", "B", 10)
            if is_ai:
                pdf.set_text_color(30, 120, 60)
            else:
                r, g, b = pdf_participant_color.get(msg.user_id, (30, 80, 160))
                pdf.set_text_color(r, g, b)
            pdf.cell(0, 7, f"{speaker}  [{ts}]",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # Message body
            pdf.set_font("DejaVu", "", 11)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(0, 6, msg.text)
            pdf.ln(3)

    buf = io.BytesIO(pdf.output())
    buf.seek(0)
    filename = f"transcript_{session_id}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
    # send_file streams via the WSGI file wrapper and supports range/conditional
    # requests, so the browser's ranged download completes instead of resetting.
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=filename)


@app.route("/transcript/<session_id>/docx")
def download_transcript_docx(session_id):
    if not _user_can_access_session(session_id, session.get("user_id")):
        return jsonify({"error": "Forbidden"}), 403

    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    messages, mode, generated_at = _transcript_data(session_id)

    doc = Document()

    # Title
    title = doc.add_heading("TogetherMindsAI — Session Transcript", level=1)
    title.runs[0].font.color.rgb = RGBColor(0x1E, 0x78, 0x40)

    # Metadata table
    meta = doc.add_paragraph()
    meta.add_run("Session ID : ").bold = True
    meta.add_run(session_id)
    meta2 = doc.add_paragraph()
    meta2.add_run("Mode       : ").bold = True
    meta2.add_run(mode)
    meta3 = doc.add_paragraph()
    meta3.add_run("Generated  : ").bold = True
    meta3.add_run(generated_at)

    doc.add_paragraph("─" * 40)

    # Assign a distinct color to each human participant (AI is always green)
    _PARTICIPANT_COLORS = [
        RGBColor(0x1E, 0x50, 0xA0),  # blue
        RGBColor(0x92, 0x27, 0x0F),  # rust red
        RGBColor(0x6B, 0x21, 0xA8),  # purple
        RGBColor(0x0D, 0x6E, 0x6E),  # teal
        RGBColor(0xB4, 0x5A, 0x09),  # amber
        RGBColor(0x1A, 0x5C, 0x2E),  # dark green
    ]
    participant_color: dict = {}
    _color_idx = 0
    for msg in messages:
        if msg.user_id != "AI" and msg.user_id not in participant_color:
            participant_color[msg.user_id] = _PARTICIPANT_COLORS[_color_idx % len(_PARTICIPANT_COLORS)]
            _color_idx += 1

    if not messages:
        p = doc.add_paragraph("No messages recorded for this session.")
        p.runs[0].italic = True
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            if is_ai:
                speaker = "AI Co-Pilot"
            elif msg.display_name:
                speaker = f"{session_id}-{msg.display_name}"
            else:
                speaker = "User"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker heading
            p = doc.add_paragraph()
            run = p.add_run(f"{speaker}  [{ts}]")
            run.bold = True
            run.font.size = Pt(10)
            if is_ai:
                run.font.color.rgb = RGBColor(0x1E, 0x78, 0x3C)
            else:
                run.font.color.rgb = participant_color.get(msg.user_id, RGBColor(0x1E, 0x50, 0xA0))

            # Message body
            doc.add_paragraph(msg.text)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    filename = f"transcript_{session_id}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.docx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
# Routes — public-key authentication API
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
@limiter.limit("10 per hour")
def api_auth_register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    public_key_b64 = (data.get("public_key") or "").strip()
    therapy_mode   = (data.get("therapy_mode") or "solo").strip()

    if not public_key_b64:
        return jsonify({"error": "public_key required"}), 400
    if therapy_mode not in ("solo", "couple", "group"):
        return jsonify({"error": "Invalid therapy_mode"}), 400
    try:
        base64.b64decode(public_key_b64, validate=True)
    except Exception:
        return jsonify({"error": "Invalid public_key encoding"}), 400

    # Registration only JOINS a clinician-led session that /session/join stashed as
    # pending_*. It never creates a new self-directed session (the sole creator is a
    # logged-in clinician via POST /therapist/start/<mode>).
    pending_couple = session.pop("pending_couple_session", None)
    pending_group  = session.pop("pending_group_session",  None)
    pending_solo   = session.pop("pending_solo_session",   None)
    joined_sid = pending_solo or pending_couple or pending_group
    if not joined_sid:
        return jsonify({"error": "No session to join"}), 400

    user_id = str(uuid.uuid4())
    db.session.add(User(id=user_id, therapy_mode=therapy_mode, public_key=public_key_b64))
    db.session.commit()
    session["user_id"] = user_id

    return jsonify({
        "user_id": user_id,
        "therapy_mode": therapy_mode,
        "session_id": joined_sid,
    }), 201


@app.route("/api/auth/challenge", methods=["POST"])
def api_auth_challenge():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    user_id = (data.get("user_id") or "").strip()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not user.public_key:
        return jsonify({"error": "No public key registered for this user"}), 400

    nonce = secrets.token_hex(32)
    user.challenge = nonce
    user.challenge_expires_at = time.time() + 300   # 5-minute window
    db.session.commit()

    return jsonify({"challenge": nonce}), 200


@app.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    user_id       = (data.get("user_id") or "").strip()
    signature_b64 = (data.get("signature") or "").strip()

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not user.challenge or not user.challenge_expires_at:
        return jsonify({"error": "No active challenge"}), 401
    if time.time() > user.challenge_expires_at:
        user.challenge = None
        user.challenge_expires_at = None
        db.session.commit()
        return jsonify({"error": "Challenge expired"}), 401

    try:
        pub_key_bytes = base64.b64decode(user.public_key)
        public_key    = load_der_public_key(pub_key_bytes)
        sig_bytes     = base64.b64decode(signature_b64)
        public_key.verify(sig_bytes, user.challenge.encode("utf-8"), ECDSA(SHA256()))
    except (InvalidSignature, Exception):
        return jsonify({"error": "Invalid signature"}), 401

    user.challenge = None
    user.challenge_expires_at = None
    db.session.commit()

    session["user_id"] = user_id

    ts = TherapySession.query.filter_by(created_by=user_id, mode=user.therapy_mode).first()
    session_id = ts.id if ts else None

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "therapy_mode": user.therapy_mode,
        "session_id": session_id,
    }), 200


# ---------------------------------------------------------------------------
# SocketIO Events
# ---------------------------------------------------------------------------

@socketio.on("join")
def on_join(data):
    try:
        session_id = data.get("session_id")
        user_id    = data.get("user_id")
        mode       = data.get("mode", "solo")

        ts = db.session.get(TherapySession, session_id)
        therapist_id = ts.therapist_id if ts else None
        eff_mode = ts.mode if (ts and ts.mode) else mode

        # Hard capacity gate: reject a client that would exceed the mode's cap
        # (solo=1, couple=2, group=unlimited). The therapist is never blocked.
        if _session_is_full(session_id, eff_mode, user_id, therapist_id):
            cap = _MODE_CLIENT_CAP.get(eff_mode)
            emit("error", {"message":
                 "This session is full — it allows up to %d participant%s."
                 % (cap, "" if cap == 1 else "s")})
            return

        join_room(session_id)
        room_mode[session_id] = mode

        # Track presence
        room_participants[session_id].add(user_id)
        sid_to_user[request.sid]    = user_id
        sid_to_session[request.sid] = session_id

        # Durable participation link (so "my sessions" finds it even if the
        # participant never sends a message).
        _record_participation(session_id, user_id)

        # Therapist co-pilot: if this session is therapist-led, remember who the
        # therapist is and — when the joiner IS the therapist — add them to the
        # private console room. Clients are never added to this room, so the
        # suggestion cards emitted there can never reach them. (ts loaded above.)
        is_therapist_led = bool(ts and ts.therapist_id)
        if is_therapist_led:
            session_therapist_id[session_id] = ts.therapist_id
            if user_id == ts.therapist_id:
                join_room(_therapist_room(session_id))
                emit("console_init", {"mode": mode})

        # Assign join position (used for default display names)
        joined = session_joined_users.setdefault(session_id, [])
        if user_id not in joined:
            joined.append(user_id)
        join_position = joined.index(user_id) + 1
        default_name  = _default_display_name(mode, join_position)

        messages = (
            ChatMessage.query
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        current_display_name = session_display_names.get(session_id, {}).get(user_id)
        emit("history", {
            "messages": [m.to_dict() for m in messages],
            "join_position": join_position,
            "default_name": default_name,
            "current_display_name": current_display_name,
        })
        emit("participant_list",
             {"participants": list(room_participants[session_id])},
             to=session_id)
        emit("participant_joined", {"user_id": user_id}, to=session_id)
    except Exception as e:
        app.logger.error("on_join error: %s", type(e).__name__)
        emit("error", {"message": "Failed to join session. Please refresh."})


@socketio.on("disconnect")
def on_disconnect():
    user_id    = sid_to_user.pop(request.sid, None)
    session_id = sid_to_session.pop(request.sid, None)
    if user_id and session_id:
        room_participants[session_id].discard(user_id)
        # session_display_names is intentionally NOT cleared on disconnect.
        # Socket.IO long-polling causes frequent disconnect/reconnect cycles;
        # clearing the name here causes the display name modal to re-appear
        # on every reconnect. session_taken_names permanently blocks reuse,
        # so keeping session_display_names across reconnects is safe.
        emit("participant_left",
             {"user_id": user_id},
             to=session_id)
        emit("participant_list",
             {"participants": list(room_participants[session_id])},
             to=session_id)


@socketio.on("send_message")
def on_send_message(data):
    try:
        session_id = data.get("session_id")
        user_id    = data.get("user_id")
        text       = data.get("text", "").strip()

        if not text:
            return
        if len(text) > _MAX_MSG_LEN:
            emit("error", {"message": (
                f"Your message is too long ({len(text):,} characters). "
                f"Please keep it under {_MAX_MSG_LEN:,} characters."
            )})
            return
        if not _check_rate_limit(user_id):
            emit("rate_limited", {"message": "You're sending messages too quickly. Please slow down."})
            return

        now  = datetime.now(timezone.utc)
        mode = room_mode.get(session_id, "solo")

        # ---- Therapist-led sessions: the AI never replies to the room. It acts
        # as a private co-pilot, emitting suggestion cards to the therapist only.
        therapist_id = session_therapist_id.get(session_id)
        if therapist_id is not None:
            display_name = session_display_names.get(session_id, {}).get(user_id)
            user_msg = ChatMessage(
                session_id=session_id, user_id=user_id, text=text,
                timestamp=now, display_name=display_name,
            )
            db.session.add(user_msg)
            db.session.add(Exercise(
                user_id=user_id, type="realtime_chat", mode=mode, timestamp=now,
            ))
            db.session.commit()

            # The conversation itself is shared with everyone in the room.
            emit("new_message",
                 {"user_id": user_id, "text": text, "display_name": display_name,
                  "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")},
                 to=session_id)
            log_event("message_sent", session_id=session_id, user_id=user_id,
                      mode=mode, message_length=len(text))

            # Every utterance drives the co-pilot — including the therapist's own,
            # so they get feedback on their interventions, not just the client's words.
            if user_id != therapist_id:
                # CLIENT spoke. Crisis safety net retained: the resources message is
                # still shown to the client (chosen "Both"), and a risk card alerts
                # the therapist. trigger_text drives the keyword risk check.
                if detect_crisis(text):
                    crisis_now = datetime.now(timezone.utc)
                    db.session.add(ChatMessage(
                        session_id=session_id, user_id="AI", text=CRISIS_RESPONSE,
                        timestamp=crisis_now,
                    ))
                    db.session.commit()
                    emit("new_message",
                         {"user_id": "AI", "text": CRISIS_RESPONSE, "display_name": None,
                          "timestamp": crisis_now.strftime("%Y-%m-%d %H:%M:%S")},
                         to=session_id)
                    log_event("crisis_detected", session_id=session_id, user_id=user_id,
                              layer="keyword", recipient="client_and_therapist")
                _run_copilot(session_id, mode, trigger_text=text)
            else:
                # THERAPIST spoke. Reflect on the intervention too. No client crisis
                # net and no keyword risk card from the therapist's own words.
                _run_copilot(session_id, mode, trigger_text=None)
            return
    except Exception as e:
        app.logger.error("on_send_message error: %s", type(e).__name__)
        db.session.rollback()
        emit("error", {"message": "Failed to send message. Please try again."})


@socketio.on("therapist_note")
def on_therapist_note(data):
    """Private note from the therapist that steers the co-pilot.

    The note is NOT broadcast to clients. It is appended to the co-pilot context
    and triggers a fresh round of suggestion cards. Rejected unless the sender is
    the session's therapist.
    """
    session_id = data.get("session_id", "")
    user_id    = data.get("user_id", "")
    text       = (data.get("text") or "").strip()
    if not text:
        return
    therapist_id = session_therapist_id.get(session_id)
    if therapist_id is None or user_id != therapist_id:
        return   # only the session therapist may add notes
    if len(text) > _MAX_MSG_LEN:
        return

    notes = session_therapist_notes.setdefault(session_id, [])
    notes.append(text)
    del notes[:-20]   # keep the most recent notes only
    _run_copilot(session_id, room_mode.get(session_id, "solo"), trigger_text=None)


@socketio.on("set_display_name")
def on_set_display_name(data):
    """First-time display name assignment for couple/group participants.

    The client emits this after the name prompt modal is confirmed.
    On success emits name_set back to the caller.
    On conflict emits name_error back to the caller only.
    """
    session_id = data.get("session_id", "")
    user_id    = data.get("user_id", "")
    name       = data.get("display_name", "").strip()

    if not name or len(name) > 40:
        emit("name_error", {"message": "Display name must be between 1 and 40 characters."})
        return

    if _is_name_taken(session_id, user_id, name):
        emit("name_error", {"message": f"'{name}' is already taken in this session. Please choose another."})
        return

    _claim_display_name(session_id, user_id, name)
    emit("name_set", {"user_id": user_id, "display_name": name})
    # Inform other participants
    emit("participant_named", {"user_id": user_id, "display_name": name}, to=session_id)


@socketio.on("rename")
def on_rename(data):
    """Rename a display name mid-session.

    On success emits name_changed to the entire room (so all bubbles update).
    On conflict emits name_error back to the caller only.
    """
    session_id = data.get("session_id", "")
    user_id    = data.get("user_id", "")
    new_name   = data.get("new_name", "").strip()

    if not new_name or len(new_name) > 40:
        emit("name_error", {"message": "Display name must be between 1 and 40 characters."})
        return

    old_name = session_display_names.get(session_id, {}).get(user_id, "")

    # No-op if same name (case-insensitive)
    if new_name.lower() == old_name.lower():
        emit("name_set", {"user_id": user_id, "display_name": new_name})
        return

    if _is_name_taken(session_id, user_id, new_name):
        emit("name_error", {"message": f"'{new_name}' is already taken in this session. Please choose another."})
        return

    _claim_display_name(session_id, user_id, new_name)
    # old_name stays in taken_names — it can never be reclaimed
    emit("name_changed", {"user_id": user_id, "old_name": old_name, "new_name": new_name}, to=session_id)


if __name__ == "__main__":
    _debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    _cert = os.path.join(os.path.dirname(__file__), "certs", "cert.pem")
    _key  = os.path.join(os.path.dirname(__file__), "certs", "key.pem")
    _ssl  = (_cert, _key) if os.path.exists(_cert) and os.path.exists(_key) else None
    if _ssl:
        app.logger.info("TLS enabled — https://localhost:5001  |  https://192.168.1.88:5001")
    else:
        app.logger.warning("certs/cert.pem or key.pem not found — running without TLS")

    socketio.run(app, debug=_debug, use_reloader=False, host="0.0.0.0", port=5001,
                 ssl_context=_ssl)
