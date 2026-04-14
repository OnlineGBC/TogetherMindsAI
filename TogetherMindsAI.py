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
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, join_room, emit
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.exceptions import InvalidSignature

from models import db, User, ChatMessage, Exercise, RateLimitEntry, TherapySession, AuditLog, init_encryption
from ai_therapist import process_input, generate_opening_message, CRISIS_RESPONSE, MEDICAL_GUARD_SAFE_RESPONSE, OFFTOPIC_SAFE_RESPONSE
from audit import log_event
from session_id import generate_session_id, normalise_join_input, rejoin_format_hint, rejoin_placeholder

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
        # One-time migration: add nickname column if it doesn't exist yet.
        # db.create_all() only creates missing tables, not missing columns.
        from sqlalchemy import text
        try:
            db.session.execute(text("ALTER TABLE therapy_sessions ADD COLUMN nickname TEXT"))
            db.session.commit()
        except Exception:
            pass  # column already exists

    # Warm up the emotion classifier in a background thread so the first
    # user message doesn't trigger a large model load mid-request, which
    # can crash the process on memory-constrained machines.
    #
    # In debug mode Flask's reloader runs two processes: a parent watcher and
    # a child that actually serves requests. WERKZEUG_RUN_MAIN is set to 'true'
    # only in the child, so we skip the warmup in the parent to avoid loading
    # the model twice.
    import threading
    _debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    if not _debug_mode:
        def _warmup_emotion_model():
            try:
                # Suppress noisy HuggingFace/transformers library output
                os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
                os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                import transformers
                transformers.logging.set_verbosity_error()
                from ai_therapist import _get_emotion_pipeline
                _get_emotion_pipeline()
                app.logger.info("Emotion model loaded and ready.")
            except Exception as exc:
                app.logger.warning("Emotion model warm-up failed (%s); will retry on first use.", exc)
        threading.Thread(target=_warmup_emotion_model, daemon=True).start()

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

    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_purge_expired_sessions, "interval", hours=24, id="purge_expired_sessions")
    _scheduler.start()
    # Run once immediately on startup to catch any sessions that expired while the app was down
    threading.Thread(target=_purge_expired_sessions, daemon=True).start()


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("home.html")


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


@app.route("/therapy/solo/<session_id>", methods=["GET"])
def therapy_solo(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="solo"))
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
                           session_id=session_id)


@app.route("/therapy/solo/<session_id>", methods=["POST"])
def therapy_solo_post(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="solo"))
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

    user_msg = ChatMessage(
        session_id=session_id, user_id=user_id, text=text, timestamp=now,
    )
    db.session.add(user_msg)

    session_message_count = len(prior_msgs) + 1
    ai_text = process_input(text, mode="solo", session_message_count=session_message_count, history=history)

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
    return render_template("couple.html", user_id=user_id, session_id=session_id)


@app.route("/therapy/group/<session_id>")
def therapy_group(session_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_get", therapy_mode="group"))
    return render_template("group.html", user_id=user_id, session_id=session_id)


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

    return render_template(
        "progress.html",
        chart_data=chart_data,
        user_id=user_id,
        therapy_mode=therapy_mode,
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

    # Step 1: exact case-sensitive lookup by session ID
    ts = db.session.get(TherapySession, raw)
    if not ts:
        # Step 2: case-insensitive nickname lookup
        ts = TherapySession.query.filter(
            db.func.lower(TherapySession.nickname) == raw.lower()
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
# Routes — session nickname (saved server-side so any device can rejoin by name)
# ---------------------------------------------------------------------------

@app.route("/session/<session_id>/nickname", methods=["POST"])
def save_session_nickname(session_id):
    ts = db.session.get(TherapySession, session_id)
    if not ts:
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(silent=True) or {}
    nickname = (data.get("nickname") or "").strip()[:60]
    ts.nickname = nickname if nickname else None
    db.session.commit()
    return jsonify({"ok": True}), 200


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


@app.route("/transcript/<session_id>/pdf")
def download_transcript_pdf(session_id):
    ts = db.session.get(TherapySession, session_id)
    if not ts or session.get("user_id") != ts.created_by:
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

    if not messages:
        pdf.set_text_color(120, 120, 120)
        pdf.set_font("DejaVu", "", 11)
        pdf.cell(0, 8, "No messages recorded for this session.",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            speaker = "AI Therapist" if is_ai else "You"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker + timestamp line
            pdf.set_font("DejaVu", "B", 10)
            if is_ai:
                pdf.set_text_color(30, 120, 60)
            else:
                pdf.set_text_color(30, 80, 160)
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
    ts = db.session.get(TherapySession, session_id)
    if not ts or session.get("user_id") != ts.created_by:
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

    if not messages:
        p = doc.add_paragraph("No messages recorded for this session.")
        p.runs[0].italic = True
    else:
        for msg in messages:
            is_ai = msg.user_id == "AI"
            speaker = "AI Therapist" if is_ai else "You"
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")

            # Speaker heading
            p = doc.add_paragraph()
            run = p.add_run(f"{speaker}  [{ts}]")
            run.bold = True
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(0x1E, 0x78, 0x3C) if is_ai else RGBColor(0x1E, 0x50, 0xA0)

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

        messages = (
            ChatMessage.query
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.timestamp.asc())
            .all()
        )
        if not messages and not config.IS_TESTING:
            opening = generate_opening_message(mode)
            ai_msg = ChatMessage(
                session_id=session_id, user_id="AI", text=opening,
                timestamp=datetime.now(timezone.utc),
            )
            db.session.add(ai_msg)
            db.session.commit()
            messages = [ai_msg]
        emit("history", {"messages": [m.to_dict() for m in messages]})
        emit("participant_list",
             {"participants": list(room_participants[session_id])},
             to=session_id)
        emit("participant_joined", {"user_id": user_id}, to=session_id)
    except Exception as e:
        app.logger.error("on_join error: %s", e)
        emit("error", {"message": "Failed to join session. Please refresh."})


@socketio.on("disconnect")
def on_disconnect():
    user_id    = sid_to_user.pop(request.sid, None)
    session_id = sid_to_session.pop(request.sid, None)
    if user_id and session_id:
        room_participants[session_id].discard(user_id)
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

        user_msg = ChatMessage(
            session_id=session_id, user_id=user_id, text=text, timestamp=now,
        )
        db.session.add(user_msg)

        session_message_count = len(prior_msgs) + 1
        ai_text = process_input(text, mode=mode, session_message_count=session_message_count, history=history)

        ai_msg = ChatMessage(
            session_id=session_id, user_id="AI", text=ai_text,
            timestamp=datetime.now(timezone.utc),
        )
        db.session.add(ai_msg)

        exercise = Exercise(
            user_id=user_id, type="realtime_chat", mode=mode, timestamp=now,
        )
        db.session.add(exercise)
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
             {"user_id": user_id, "text": text, "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")},
             to=session_id)
        emit("new_message",
             {"user_id": "AI", "text": ai_text,
              "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")},
             to=session_id)
    except Exception as e:
        app.logger.error("on_send_message error: %s", e)
        db.session.rollback()
        emit("error", {"message": "Failed to send message. Please try again."})


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
