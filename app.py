import os
os.environ["EVENTLET_HUB"] = "poll"
import eventlet
eventlet.monkey_patch()

import random
import time
import uuid
from collections import defaultdict
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, emit
from werkzeug.security import generate_password_hash

from models import db, User, ChatMessage, Exercise
from ai_therapist import process_input

app = Flask(__name__)
app.config["SECRET_KEY"] = "togethermindsai-secret-key-2024"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///togethermindsai.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

# In-memory map of session_id → therapy mode (populated on SocketIO join)
room_mode = {}

# ---------------------------------------------------------------------------
# Rate limiting — sliding window, in-memory
# ---------------------------------------------------------------------------
_rate_store: dict = defaultdict(list)
_RATE_WINDOW = 60      # seconds
_RATE_MAX_MSGS = 20    # max messages per user per window


def _check_rate_limit(user_id: str) -> bool:
    """Return True if the user is within the allowed rate, False if exceeded."""
    now = time.time()
    cutoff = now - _RATE_WINDOW
    _rate_store[user_id] = [t for t in _rate_store[user_id] if t > cutoff]
    if len(_rate_store[user_id]) >= _RATE_MAX_MSGS:
        return False
    _rate_store[user_id].append(now)
    return True

with app.app_context():
    db.create_all()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/auth/<therapy_mode>", methods=["GET"])
def auth_get(therapy_mode):
    return render_template("auth.html", therapy_mode=therapy_mode)


@app.route("/auth/<therapy_mode>", methods=["POST"])
def auth_post(therapy_mode):
    passphrase = request.form.get("passphrase", "").strip()
    user_id = str(uuid.uuid4())

    user = User(
        id=user_id,
        passphrase_hash=generate_password_hash(passphrase) if passphrase else None,
        therapy_mode=therapy_mode,
    )
    db.session.add(user)
    db.session.commit()

    session["user_id"] = user_id

    if therapy_mode == "solo":
        return redirect(url_for("therapy_solo", user_id=user_id))
    elif therapy_mode == "couple":
        return redirect(url_for("therapy_couple", user_id=user_id))
    else:  # group
        random_session_id = str(random.randint(1000, 9999))
        return redirect(url_for("therapy_group", user_id=user_id, session_id=random_session_id))


@app.route("/therapy/solo/<user_id>", methods=["GET"])
def therapy_solo(user_id):
    messages = (
        ChatMessage.query
        .filter_by(session_id=user_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    return render_template("solo.html", messages=messages, user_id=user_id)


@app.route("/therapy/solo/<user_id>", methods=["POST"])
def therapy_solo_post(user_id):
    text = request.form.get("message", "").strip()
    if not text:
        return redirect(url_for("therapy_solo", user_id=user_id))

    if not _check_rate_limit(user_id):
        return redirect(url_for("therapy_solo", user_id=user_id))

    user_msg = ChatMessage(
        session_id=user_id,
        user_id=user_id,
        text=text,
        timestamp=datetime.utcnow(),
    )
    db.session.add(user_msg)

    session_message_count = ChatMessage.query.filter_by(session_id=user_id).count()
    ai_text = process_input(text, mode="solo", session_message_count=session_message_count)
    ai_msg = ChatMessage(
        session_id=user_id,
        user_id="AI",
        text=ai_text,
        timestamp=datetime.utcnow(),
    )
    db.session.add(ai_msg)

    # Record exercise for progress tracking
    exercise = Exercise(
        user_id=user_id,
        type="solo_chat",
        prompt=text,
        response=ai_text,
        timestamp=datetime.utcnow(),
    )
    db.session.add(exercise)

    db.session.commit()
    return redirect(url_for("therapy_solo", user_id=user_id))


@app.route("/therapy/couple/<user_id>")
def therapy_couple(user_id):
    return render_template("couple.html", user_id=user_id)


@app.route("/therapy/group/<user_id>/<session_id>")
def therapy_group(user_id, session_id):
    return render_template("group.html", user_id=user_id, session_id=session_id)


@app.route("/progress/<user_id>/<therapy_mode>")
def progress(user_id, therapy_mode):
    exercises = (
        Exercise.query
        .filter_by(user_id=user_id)
        .order_by(Exercise.timestamp.asc())
        .all()
    )

    # Group by ISO week
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
# SocketIO Events
# ---------------------------------------------------------------------------

@socketio.on("join")
def on_join(data):
    session_id = data.get("session_id")
    user_id = data.get("user_id")
    mode = data.get("mode", "solo")

    join_room(session_id)
    room_mode[session_id] = mode

    messages = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )
    history = [m.to_dict() for m in messages]
    emit("history", {"messages": history})


@socketio.on("send_message")
def on_send_message(data):
    session_id = data.get("session_id")
    user_id = data.get("user_id")
    text = data.get("text", "").strip()

    if not text:
        return

    if not _check_rate_limit(user_id):
        emit("rate_limited", {"message": "You're sending messages too quickly. Please slow down."})
        return

    now = datetime.utcnow()

    user_msg = ChatMessage(
        session_id=session_id,
        user_id=user_id,
        text=text,
        timestamp=now,
    )
    db.session.add(user_msg)

    mode = room_mode.get(session_id, "solo")
    session_message_count = ChatMessage.query.filter_by(session_id=session_id).count()
    ai_text = process_input(text, mode=mode, session_message_count=session_message_count)
    ai_msg = ChatMessage(
        session_id=session_id,
        user_id="AI",
        text=ai_text,
        timestamp=datetime.utcnow(),
    )
    db.session.add(ai_msg)

    # Record exercise for the real user
    exercise = Exercise(
        user_id=user_id,
        type="realtime_chat",
        prompt=text,
        response=ai_text,
        timestamp=now,
    )
    db.session.add(exercise)

    db.session.commit()

    emit(
        "new_message",
        {"user_id": user_id, "text": text, "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")},
        to=session_id,
    )
    emit(
        "new_message",
        {"user_id": "AI", "text": ai_text, "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")},
        to=session_id,
    )


if __name__ == "__main__":
    socketio.run(app, debug=True, use_reloader=False, host="0.0.0.0", port=5001)
