import os
import config  # loads .env and exposes typed constants

# eventlet must be monkey-patched before any other imports when used
if config.ASYNC_MODE == "eventlet":
    import eventlet
    eventlet.monkey_patch()

import base64
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import io
import smtplib
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response, flash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, join_room, emit
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.exceptions import InvalidSignature

from models import db, User, ChatMessage, Exercise, RateLimitEntry, TherapySession, AuditLog, init_encryption
from ai_therapist import (
    process_input, generate_opening_message, generate_silence_nudge,
    contains_referral, strip_referral_sentences, detect_escalation,
    CRISIS_RESPONSE, MEDICAL_GUARD_SAFE_RESPONSE, OFFTOPIC_SAFE_RESPONSE,
)
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
_RETENTION_DAYS  = 30
_RETENTION_DELTA = timedelta(days=_RETENTION_DAYS)



# In-memory maps (ephemeral — reset on restart, which is acceptable for these)
room_mode: dict = {}
room_participants: dict = defaultdict(set)   # session_id → set of user_ids
sid_to_user: dict = {}                        # SocketIO SID → user_id
sid_to_session: dict = {}                     # SocketIO SID → session_id

# Display name tracking — all ephemeral, reset on server restart
session_display_names: dict = {}  # session_id → {user_id: display_name}
session_taken_names: dict   = {}  # session_id → set of lowercased names (permanent for session lifetime)
session_joined_users: dict  = {}  # session_id → ordered list of user_ids (join order, for default names)
session_opening_sent: set        = set()  # session_ids that have already had their opening message generated
session_ai_last_response: dict   = {}    # session_id → datetime of last AI response (cooldown guard)
session_escalation_hint_sent: set = set() # session_ids where Claude has been invited to make the referral once
session_last_message_at: dict    = {}    # session_id → datetime of last message (user OR AI), used for silence nudge
session_silence_check_pending: set = set() # session_ids with a silence check task scheduled (debounces)


def _default_display_name(mode: str, position: int) -> str:
    """Return the default display name for a participant given their join position."""
    if mode == "couple":
        return f"Partner{position}"
    elif mode == "group":
        return f"GroupMember{position}"
    return f"Solo{position}"


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
            pass  # column already removed or never existed

    # Load the emotion classifier synchronously at startup so no request ever
    # triggers a mid-request model load.  The app runs with use_reloader=False
    # so there is no parent/child process split — always load here.
    import threading
    try:
        os.environ["TRANSFORMERS_VERBOSITY"] = "error"
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        import transformers
        transformers.logging.set_verbosity_error()
        from ai_therapist import _get_emotion_pipeline
        _get_emotion_pipeline()
        print("Emotion model loaded and ready.", flush=True)
    except Exception as exc:
        import traceback
        print(f"WARNING: Emotion model warm-up failed ({exc}); will load on first use.", flush=True)
        traceback.print_exc()

    # Add retention_expires_at column if it doesn't exist yet (one-time migration)
    with app.app_context():
        from sqlalchemy import text
        try:
            db.session.execute(text(
                "ALTER TABLE therapy_sessions ADD COLUMN retention_expires_at DATETIME"
            ))
            db.session.commit()
        except Exception:
            pass  # column already exists

    # Add display_name column to chat_messages for participant identification
    with app.app_context():
        from sqlalchemy import text
        try:
            db.session.execute(text(
                "ALTER TABLE chat_messages ADD COLUMN display_name VARCHAR(60)"
            ))
            db.session.commit()
        except Exception:
            pass  # column already exists

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


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/tos")
def tos():
    return render_template("tos.html")


@app.route("/auth/<therapy_mode>", methods=["GET"])
def auth_get(therapy_mode):
    return render_template("auth.html", therapy_mode=therapy_mode)


@app.route("/auth/<therapy_mode>", methods=["POST"])
@limiter.limit("10 per hour")
def auth_post(therapy_mode):
    """Legacy form-based auth kept as fallback. New users go through /api/auth/register."""
    user_id = str(uuid.uuid4())
    user = User(id=user_id, therapy_mode=therapy_mode)
    db.session.add(user)

    # Check if this auth is for joining an existing session (not creating a new one)
    pending_couple = session.pop("pending_couple_session", None)
    pending_group  = session.pop("pending_group_session",  None)

    new_session_id = None
    if therapy_mode == "solo" and not pending_couple and not pending_group:
        new_session_id = generate_session_id()
        db.session.add(TherapySession(
            id=new_session_id, mode="solo", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))
    elif therapy_mode == "couple" and not pending_couple:
        new_session_id = generate_session_id()
        db.session.add(TherapySession(
            id=new_session_id, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))
    elif therapy_mode == "group" and not pending_group:
        new_session_id = generate_session_id()
        db.session.add(TherapySession(
            id=new_session_id, mode="group", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))

    db.session.commit()
    session["user_id"] = user_id

    if new_session_id:
        log_event("session_created", session_id=new_session_id, user_id=user_id,
                  mode=therapy_mode)

    if therapy_mode == "solo":
        return redirect(url_for("therapy_solo", session_id=new_session_id))
    elif therapy_mode == "couple":
        sid = pending_couple or new_session_id
        return redirect(url_for("therapy_couple", session_id=sid))
    else:
        sid = pending_group or new_session_id
        return redirect(url_for("therapy_group", session_id=sid))


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


@app.route("/therapy/solo/<session_id>", methods=["GET"])
def therapy_solo(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="solo"))
    if not _session_exists(session_id):
        return _redirect_invalid_session()

    # Assign join position for solo (always position 1 — only one participant)
    joined = session_joined_users.setdefault(session_id, [])
    if user_id not in joined:
        joined.append(user_id)
    join_position = joined.index(user_id) + 1
    default_name = _default_display_name("solo", join_position)

    messages = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    if not messages and not config.IS_TESTING:
        opening = generate_opening_message("solo")
        msg = ChatMessage(
            session_id=session_id, user_id="AI", text=opening,
            timestamp=datetime.now(timezone.utc),
        )
        db.session.add(msg)
        db.session.commit()
        messages = [msg]
    return render_template("solo.html", messages=messages, user_id=user_id,
                           session_id=session_id, default_name=default_name)


@app.route("/therapy/solo/<session_id>", methods=["POST"])
def therapy_solo_post(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="solo"))
    if not _session_exists(session_id):
        return _redirect_invalid_session()
    text = request.form.get("message", "").strip()
    if not text:
        return redirect(url_for("therapy_solo", session_id=session_id))

    def _solo_error(msg, draft=None):
        messages = (
            ChatMessage.query
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        return render_template("solo.html", messages=messages, user_id=user_id,
                               session_id=session_id, error=msg, draft=draft), 422

    if len(text) > _MAX_MSG_LEN:
        return _solo_error(
            f"Your message is too long ({len(text):,} characters). "
            f"Please keep it under {_MAX_MSG_LEN:,} characters.",
            draft=text,
        )
    if not _check_rate_limit(user_id):
        return _solo_error("You're sending messages too quickly — please wait a moment.")

    now = datetime.now(timezone.utc)

    # Fetch conversation history before adding the new message
    prior_msgs = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    history = [
        {"role": "assistant" if m.user_id == "AI" else "user", "content": m.text}
        for m in prior_msgs
    ]

    display_name = session_display_names.get(session_id, {}).get(user_id)
    user_msg = ChatMessage(
        session_id=session_id, user_id=user_id, text=text,
        timestamp=now, display_name=display_name,
    )
    db.session.add(user_msg)

    session_message_count = len(prior_msgs) + 1
    hint_already_sent = session_id in session_escalation_hint_sent
    if not hint_already_sent and (detect_escalation(text) or session_message_count >= 10):
        session_escalation_hint_sent.add(session_id)

    ai_text = process_input(
        text, mode="solo",
        session_message_count=session_message_count,
        history=history,
        referral_already_made=hint_already_sent,
    )
    if hint_already_sent:
        ai_text = strip_referral_sentences(ai_text)

    ai_msg = ChatMessage(
        session_id=session_id, user_id="AI", text=ai_text,
        timestamp=datetime.now(timezone.utc),
    )
    db.session.add(ai_msg)

    exercise = Exercise(
        user_id=user_id, type="solo_chat", mode="solo", timestamp=now,
    )
    db.session.add(exercise)
    db.session.commit()

    log_event("message_sent", session_id=session_id, user_id=user_id,
              mode="solo", message_length=len(text))
    if ai_text == CRISIS_RESPONSE:
        log_event("crisis_detected", session_id=session_id, user_id=user_id, layer="keyword_or_claude")
    elif ai_text == MEDICAL_GUARD_SAFE_RESPONSE:
        log_event("medical_guard_fired", session_id=session_id, user_id=user_id)
    elif ai_text == OFFTOPIC_SAFE_RESPONSE:
        log_event("offtopic_deflected", session_id=session_id, user_id=user_id, mode="solo")

    return redirect(url_for("therapy_solo", session_id=session_id))


@app.route("/therapy/couple/<session_id>")
def therapy_couple(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="couple"))
    if not _session_exists(session_id):
        return _redirect_invalid_session()
    return render_template(
        "couple.html",
        user_id=user_id, session_id=session_id,
    )


@app.route("/therapy/group/<session_id>")
def therapy_group(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="group"))
    if not _session_exists(session_id):
        return _redirect_invalid_session()
    return render_template(
        "group.html",
        user_id=user_id, session_id=session_id,
    )


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
        "solo": "Solo Reflection",
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
        session["user_id"] = ts.created_by
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
# Routes — session deletion (user-initiated, from End Session modal)
# ---------------------------------------------------------------------------

@app.route("/session/<session_id>/delete", methods=["POST"])
def delete_session(session_id):
    """Delete all data for a session and return to home."""
    ts = db.session.get(TherapySession, session_id)
    user_id = ts.created_by if ts else None
    ChatMessage.query.filter_by(session_id=session_id).delete(synchronize_session=False)
    TherapySession.query.filter_by(id=session_id).delete(synchronize_session=False)
    if user_id:
        Exercise.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        User.query.filter_by(id=user_id).delete(synchronize_session=False)
    db.session.commit()
    log_event("session_deleted_user", session_id=session_id, user_id=user_id,
              trigger="user")
    session.clear()
    return jsonify({"deleted": True}), 200


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
                speaker = "AI Therapist"
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
    filename = f"transcript_{session_id}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


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
                speaker = "AI Therapist"
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
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


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

    # Check if this auth is for joining an existing session (not creating a new one)
    pending_couple = session.pop("pending_couple_session", None)
    pending_group  = session.pop("pending_group_session",  None)

    user_id = str(uuid.uuid4())
    user = User(id=user_id, therapy_mode=therapy_mode, public_key=public_key_b64)
    db.session.add(user)

    response_data = {"user_id": user_id, "therapy_mode": therapy_mode}

    if therapy_mode == "solo" and not pending_couple and not pending_group:
        new_sid = generate_session_id()
        db.session.add(TherapySession(
            id=new_sid, mode="solo", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))
        response_data["session_id"] = new_sid
    elif therapy_mode == "couple" and not pending_couple:
        new_sid = generate_session_id()
        db.session.add(TherapySession(
            id=new_sid, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))
        response_data["session_id"] = new_sid
    elif therapy_mode == "group" and not pending_group:
        new_sid = generate_session_id()
        db.session.add(TherapySession(
            id=new_sid, mode="group", created_by=user_id,
            created_at=datetime.now(timezone.utc),
            retention_expires_at=datetime.now(timezone.utc) + _RETENTION_DELTA,
        ))
        response_data["session_id"] = new_sid

    if pending_couple:
        response_data["session_id"] = pending_couple
    if pending_group:
        response_data["session_id"] = pending_group

    db.session.commit()
    session["user_id"] = user_id

    created_sid = response_data.get("session_id")
    if created_sid and not pending_couple and not pending_group:
        log_event("session_created", session_id=created_sid, user_id=user_id,
                  mode=therapy_mode)

    return jsonify(response_data), 201


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

        join_room(session_id)
        room_mode[session_id] = mode

        # Track presence
        room_participants[session_id].add(user_id)
        sid_to_user[request.sid]    = user_id
        sid_to_session[request.sid] = session_id

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
        if not messages and not config.IS_TESTING and session_id not in session_opening_sent:
            session_opening_sent.add(session_id)
            # Pre-claim the AI cooldown slot so any partner message arriving
            # while the opening message is generating correctly sees an active
            # cooldown and does not fire a second consecutive AI response.
            # Released in `finally` once the opening is committed so the user's
            # first reply gets a normal AI response (without the release, the
            # 20s cooldown silently swallowed the user's first message).
            session_ai_last_response[session_id] = datetime.now(timezone.utc)
            try:
                opening = generate_opening_message(mode)
                ai_msg = ChatMessage(
                    session_id=session_id, user_id="AI", text=opening,
                    timestamp=datetime.now(timezone.utc),
                )
                db.session.add(ai_msg)
                db.session.commit()
                messages = [ai_msg]
                # AI opening counts as the first message — start the silence-nudge clock
                # so the room gets a check-in if no one engages.
                _schedule_silence_check(session_id, mode)
            finally:
                session_ai_last_response.pop(session_id, None)
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


# ---------------------------------------------------------------------------
# Silence nudge — couple/group only. After SILENCE_NUDGE_SECONDS of total
# silence (no user OR AI messages), the AI sends a brief re-engagement line.
# ---------------------------------------------------------------------------

def _emit_silence_nudge_scheduled(session_id: str, deadline: datetime) -> None:
    """Tell connected clients when the silence nudge will fire so they can show
    a countdown chip. The client renders the timer locally — the server doesn't
    spam per-second updates."""
    socketio.emit("silence_nudge_scheduled", {
        "deadline_at": deadline.isoformat(),
        "seconds":     config.SILENCE_NUDGE_SECONDS,
    }, to=session_id)


def _schedule_silence_check(session_id: str, mode: str) -> None:
    """Mark the session active and schedule one silence check task if none is
    already pending. Couple/group only; solo and disabled (=0) modes are no-ops."""
    if mode not in ("couple", "group"):
        return
    if config.SILENCE_NUDGE_SECONDS <= 0:
        return
    now = datetime.now(timezone.utc)
    session_last_message_at[session_id] = now
    if session_id in session_silence_check_pending:
        # A check is already armed; the existing task will see the updated
        # last_message_at and reset its own deadline. No need to schedule again.
        # Still emit the new deadline so all clients reset their countdown UI.
        deadline = now + timedelta(seconds=config.SILENCE_NUDGE_SECONDS)
        _emit_silence_nudge_scheduled(session_id, deadline)
        return
    session_silence_check_pending.add(session_id)
    deadline = now + timedelta(seconds=config.SILENCE_NUDGE_SECONDS)
    _emit_silence_nudge_scheduled(session_id, deadline)
    socketio.start_background_task(_silence_check_task, session_id, mode, app.app_context)


def _silence_check_task(session_id: str, mode: str, app_context_factory) -> None:
    """Wait for a quiet period; if still quiet when we wake, send a nudge.

    Loops while the silence window keeps getting reset by new messages, then
    exits cleanly the moment it finds either (a) a long-enough silence, or
    (b) an empty room. After firing a nudge, the task does NOT re-arm — the
    flag is only cleared so the next user message can schedule a fresh check.
    """
    try:
        while True:
            socketio.sleep(config.SILENCE_NUDGE_SECONDS)
            last = session_last_message_at.get(session_id)
            if last is None:
                # Session state was cleared (e.g. server restart cleanup). Stop.
                return
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < config.SILENCE_NUDGE_SECONDS:
                # Someone spoke during the wait — keep watching from the new mark.
                continue
            if not room_participants.get(session_id):
                # Empty room — don't nudge into the void.
                return
            with app_context_factory():
                _send_silence_nudge(session_id, mode)
                return
    finally:
        session_silence_check_pending.discard(session_id)


def _send_silence_nudge(session_id: str, mode: str) -> None:
    """Generate, persist, and broadcast a silence nudge from the AI.

    Important: this does NOT update session_ai_last_response. The cooldown
    is for protecting human-to-human exchanges; a nudge into a silent room
    isn't part of that, and updating the cooldown would block the user's
    follow-up reply for another 20s.
    """
    prior_msgs = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    history = [
        {"role": "assistant" if m.user_id == "AI" else "user", "content": m.text}
        for m in prior_msgs
    ]
    nudge_text = generate_silence_nudge(mode, history=history)
    now = datetime.now(timezone.utc)
    nudge_msg = ChatMessage(
        session_id=session_id, user_id="AI", text=nudge_text, timestamp=now,
    )
    db.session.add(nudge_msg)
    db.session.commit()
    session_last_message_at[session_id] = now
    socketio.emit("new_message", {
        "user_id":      "AI",
        "text":         nudge_text,
        "display_name": None,
        "timestamp":    now.strftime("%Y-%m-%d %H:%M:%S"),
    }, to=session_id)


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

        # AI response cooldown — prevent consecutive AI messages when partners
        # send messages in rapid succession (couple/group only; solo always responds).
        # The slot is pre-claimed immediately (before the Claude call) so that any
        # concurrent handler arriving while Claude is processing correctly sees an
        # active cooldown and skips — no race condition.
        _AI_COOLDOWN_SECONDS = config.AI_COOLDOWN_SECONDS
        skip_ai_response = False
        if mode in ("couple", "group"):
            last_ai = session_ai_last_response.get(session_id)
            if last_ai and (now - last_ai).total_seconds() < _AI_COOLDOWN_SECONDS:
                skip_ai_response = True
            else:
                session_ai_last_response[session_id] = now

        # Fetch conversation history before adding the new message
        prior_msgs = (
            ChatMessage.query
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        history = [
            {"role": "assistant" if m.user_id == "AI" else "user", "content": m.text}
            for m in prior_msgs
        ]

        display_name = session_display_names.get(session_id, {}).get(user_id)
        user_msg = ChatMessage(
            session_id=session_id, user_id=user_id, text=text,
            timestamp=now, display_name=display_name,
        )
        db.session.add(user_msg)

        exercise = Exercise(
            user_id=user_id, type="realtime_chat", mode=mode, timestamp=now,
        )
        db.session.add(exercise)

        # Always broadcast the user's own message
        emit("new_message",
             {"user_id": user_id, "text": text, "display_name": display_name,
              "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")},
             to=session_id)

        if skip_ai_response:
            # Another AI response fired within the cooldown window — save user
            # message without generating a new AI reply to avoid consecutive responses
            db.session.commit()
            log_event("message_sent", session_id=session_id, user_id=user_id,
                      mode=mode, message_length=len(text))
            # User activity still resets the silence-nudge clock even when
            # the AI itself doesn't reply, so partners aren't nudged
            # mid-conversation just because the cooldown ate the AI reply.
            _schedule_silence_check(session_id, mode)
            return

        session_message_count = len(prior_msgs) + 1
        hint_already_sent = session_id in session_escalation_hint_sent
        if not hint_already_sent and (detect_escalation(text) or session_message_count >= 10):
            session_escalation_hint_sent.add(session_id)

        ai_text = process_input(
            text, mode=mode,
            session_message_count=session_message_count,
            history=history,
            referral_already_made=hint_already_sent,
        )
        if hint_already_sent:
            ai_text = strip_referral_sentences(ai_text)

        ai_msg = ChatMessage(
            session_id=session_id, user_id="AI", text=ai_text,
            timestamp=datetime.now(timezone.utc),
        )
        db.session.add(ai_msg)
        db.session.commit()

        log_event("message_sent", session_id=session_id, user_id=user_id,
                  mode=mode, message_length=len(text))
        if ai_text == CRISIS_RESPONSE:
            log_event("crisis_detected", session_id=session_id, user_id=user_id,
                      layer="keyword_or_claude")
        elif ai_text == MEDICAL_GUARD_SAFE_RESPONSE:
            log_event("medical_guard_fired", session_id=session_id, user_id=user_id)
        elif ai_text == OFFTOPIC_SAFE_RESPONSE:
            log_event("offtopic_deflected", session_id=session_id, user_id=user_id, mode=mode)

        emit("new_message",
             {"user_id": "AI", "text": ai_text, "display_name": None,
              "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")},
             to=session_id)
        # AI just spoke — start the silence-nudge clock.
        _schedule_silence_check(session_id, mode)
    except Exception as e:
        app.logger.error("on_send_message error: %s", type(e).__name__)
        db.session.rollback()
        emit("error", {"message": "Failed to send message. Please try again."})


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
