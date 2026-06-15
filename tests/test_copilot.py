"""
tests/test_copilot.py
---------------------
Tests for the therapist co-pilot (Phase 0):

  - copilot.generate_suggestions: JSON parsing, graceful failure, silence
  - copilot.build_risk_cards: crisis / escalation / benign
  - copilot.dedupe_cards
  - SocketIO behaviour in a therapist-led session:
      * suggestion cards reach ONLY the therapist, never clients (channel isolation)
      * the AI never auto-replies to the room
      * crisis = "Both": client still sees the resources message AND the therapist
        gets a risk card
  - regression: non-therapist sessions still drive the AI (process_input) as before
"""

import os
import sys
import json
import uuid
import base64
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-copilot")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import copilot
from TogetherMindsAI import app, socketio, session_therapist_id
from models import db, init_encryption, TherapySession
from ai_therapist import CRISIS_RESPONSE
from session_id import generate_session_id

init_encryption(TEST_KEY)


def _insert_solo_session(therapist_id=None, created_by="owner-xyz"):
    """Insert a solo TherapySession (therapist-led iff therapist_id given) and return its id."""
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by=created_by,
        created_at=datetime.now(timezone.utc),
        retention_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        therapist_id=therapist_id,
    ))
    db.session.commit()
    return sid


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def enc_client():
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db._app_engines[app] = {None: test_engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _pub():
    """A valid base64 string accepted by /api/auth/register (need not be a real key)."""
    return base64.b64encode(uuid.uuid4().bytes).decode()


def _register(client, mode="couple", as_therapist=False):
    body = {"public_key": _pub(), "therapy_mode": mode}
    if as_therapist:
        body["as_therapist"] = True
    rv = client.post("/api/auth/register", json=body)
    assert rv.status_code == 201, rv.get_data(as_text=True)
    data = rv.get_json()
    return data["user_id"], data["session_id"]


def _claude_returning(text):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    cl = MagicMock()
    cl.messages.create.return_value = msg
    return cl


def _names(received):
    return [e["name"] for e in received]


def _args_of(received, event_name):
    return [e["args"][0] for e in received if e["name"] == event_name]


# ---------------------------------------------------------------------------
# generate_suggestions
# ---------------------------------------------------------------------------

def test_generate_suggestions_parses_cards():
    raw = json.dumps([{"type": "question", "text": "Ask about sleep.", "confidence": 0.9}])
    with patch("copilot._get_claude_client", return_value=_claude_returning(raw)):
        cards = copilot.generate_suggestions("Client: I'm exhausted", mode="solo")
    assert len(cards) == 1
    assert cards[0]["type"] == "question"
    assert cards[0]["text"] == "Ask about sleep."


def test_generate_suggestions_empty_on_api_error():
    bad = MagicMock()
    bad.messages.create.side_effect = RuntimeError("API down")
    with patch("copilot._get_claude_client", return_value=bad):
        assert copilot.generate_suggestions("Client: hello", mode="solo") == []


def test_generate_suggestions_silence_when_model_returns_empty_array():
    with patch("copilot._get_claude_client", return_value=_claude_returning("[]")):
        assert copilot.generate_suggestions("Client: hello", mode="solo") == []


def test_generate_suggestions_empty_transcript_makes_no_call():
    # Whitespace-only transcript short-circuits before any API call.
    assert copilot.generate_suggestions("   ", mode="solo") == []


def test_generate_suggestions_strips_code_fence():
    raw = "```json\n[{\"type\":\"technique\",\"text\":\"Box breathing now.\",\"confidence\":0.7}]\n```"
    with patch("copilot._get_claude_client", return_value=_claude_returning(raw)):
        cards = copilot.generate_suggestions("Client: panicking", mode="solo")
    assert cards and cards[0]["type"] == "technique"


def test_generate_suggestions_filters_non_suggestion_types():
    # The model must not be able to inject a "risk" card — only build_risk_cards may.
    raw = json.dumps([
        {"type": "risk", "text": "fake risk", "confidence": 1.0},
        {"type": "question", "text": "A real question.", "confidence": 0.5},
    ])
    with patch("copilot._get_claude_client", return_value=_claude_returning(raw)):
        cards = copilot.generate_suggestions("Client: hi", mode="solo")
    assert [c["type"] for c in cards] == ["question"]


def test_generate_suggestions_garbage_output_is_empty():
    with patch("copilot._get_claude_client", return_value=_claude_returning("not json at all")):
        assert copilot.generate_suggestions("Client: hi", mode="solo") == []


# ---------------------------------------------------------------------------
# build_risk_cards
# ---------------------------------------------------------------------------

def test_build_risk_cards_crisis_high_priority():
    cards = copilot.build_risk_cards("I want to kill myself")
    assert len(cards) == 1
    assert cards[0]["type"] == "risk"
    assert cards[0]["priority"] == "high"


def test_build_risk_cards_escalation_medium_priority():
    cards = copilot.build_risk_cards("I think I need medication for this")
    assert len(cards) == 1
    assert cards[0]["type"] == "risk"
    assert cards[0]["priority"] == "medium"


def test_build_risk_cards_benign_none():
    assert copilot.build_risk_cards("I had a pleasant walk in the park today") == []


# ---------------------------------------------------------------------------
# dedupe_cards
# ---------------------------------------------------------------------------

def test_dedupe_cards_drops_recently_shown():
    shown = ["Ask about sleep."]
    cards = [
        {"type": "question", "text": "Ask about sleep.", "confidence": 0.8},
        {"type": "question", "text": "Ask about appetite.", "confidence": 0.8},
    ]
    out = copilot.dedupe_cards(cards, shown)
    assert [c["text"] for c in out] == ["Ask about appetite."]


def test_dedupe_cards_is_whitespace_insensitive():
    shown = ["ask about   SLEEP."]
    cards = [{"type": "question", "text": "Ask about sleep.", "confidence": 0.8}]
    assert copilot.dedupe_cards(cards, shown) == []


# ---------------------------------------------------------------------------
# SocketIO — therapist-led session behaviour
# ---------------------------------------------------------------------------

def _join_pair(client, mode="couple"):
    """Create a therapist-led session and connect a therapist + client socket.

    Returns (therapist_sio, client_sio, session_id, client_user_id).
    """
    therapist_id, sid = _register(client, mode=mode, as_therapist=True)
    client_user = str(uuid.uuid4())

    t_sio = socketio.test_client(app, flask_test_client=client)
    t_sio.emit("join", {"session_id": sid, "user_id": therapist_id, "mode": mode})
    c_sio = socketio.test_client(app, flask_test_client=client)
    c_sio.emit("join", {"session_id": sid, "user_id": client_user, "mode": mode})
    t_sio.get_received()   # drain join/history noise
    c_sio.get_received()
    return t_sio, c_sio, sid, client_user


def test_suggestion_cards_reach_only_therapist(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    fake = [{"type": "question", "text": "Ask what 'stuck' means to them.", "confidence": 0.8}]
    with patch("copilot.generate_suggestions", return_value=fake):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "I feel stuck lately", "mode": "couple"})

    t_recv = t_sio.get_received()
    c_recv = c_sio.get_received()

    # Channel isolation: only the therapist gets the private cards.
    assert "suggestion_cards" in _names(t_recv)
    assert "suggestion_cards" not in _names(c_recv)
    # The conversation itself is shared with everyone.
    assert "new_message" in _names(t_recv)
    assert "new_message" in _names(c_recv)


def test_no_ai_autoreply_in_therapist_led(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    with patch("copilot.generate_suggestions", return_value=[]):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "Today was a calm and ordinary day", "mode": "couple"})

    c_new = _args_of(c_sio.get_received(), "new_message")
    # The client sees their own message echoed, but no AI reply to the room.
    assert any(m["user_id"] == client_user for m in c_new)
    assert all(m["user_id"] != "AI" for m in c_new)


def test_crisis_shows_client_message_and_therapist_risk_card(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    with patch("copilot.generate_suggestions", return_value=[]):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "I want to kill myself", "mode": "couple"})

    t_recv = t_sio.get_received()
    c_recv = c_sio.get_received()

    # "Both": the client still sees the crisis-resources safety-net message.
    c_new = _args_of(c_recv, "new_message")
    assert any(m["user_id"] == "AI" and m["text"] == CRISIS_RESPONSE for m in c_new)

    # The therapist gets a high-priority risk card; the client gets no cards.
    t_cards = _args_of(t_recv, "suggestion_cards")
    assert t_cards and any(card["type"] == "risk" for card in t_cards[0]["cards"])
    assert "suggestion_cards" not in _names(c_recv)


def test_therapist_note_emits_cards_to_therapist_only(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    fake = [{"type": "observation", "text": "Possible avoidance pattern.", "confidence": 0.6}]
    with patch("copilot.generate_suggestions", return_value=fake):
        t_sio.emit("therapist_note", {"session_id": sid, "user_id": session_therapist_id[sid],
                                      "text": "They keep changing the subject."})

    assert "suggestion_cards" in _names(t_sio.get_received())
    # The note never surfaces to the client as a message or a card.
    c_recv = c_sio.get_received()
    assert "suggestion_cards" not in _names(c_recv)
    assert "new_message" not in _names(c_recv)


def test_therapist_note_rejected_from_non_therapist(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    with patch("copilot.generate_suggestions", return_value=[{"type": "question",
               "text": "x", "confidence": 0.5}]) as gen:
        c_sio.emit("therapist_note", {"session_id": sid, "user_id": client_user,
                                      "text": "I am pretending to be the therapist"})
        gen.assert_not_called()
    assert "suggestion_cards" not in _names(t_sio.get_received())


def test_non_therapist_session_still_drives_ai(enc_client):
    """Regression: a normal (non-therapist) couple session is unchanged — the AI
    still replies via process_input."""
    user_id, sid = _register(enc_client, mode="couple", as_therapist=False)
    c_sio = socketio.test_client(app, flask_test_client=enc_client)
    c_sio.emit("join", {"session_id": sid, "user_id": user_id, "mode": "couple"})
    c_sio.get_received()

    assert session_therapist_id.get(sid) is None
    with patch("TogetherMindsAI.process_input", return_value="A supportive reply.") as mock_pi:
        c_sio.emit("send_message", {"session_id": sid, "user_id": user_id,
                                    "text": "Just checking in", "mode": "couple"})
    mock_pi.assert_called()


# ---------------------------------------------------------------------------
# Therapist-led 1:1 (solo) — distinct client identity + realtime co-pilot
# ---------------------------------------------------------------------------

def test_therapist_led_solo_join_gives_client_own_identity(enc_client):
    """A client joining a therapist-led 1:1 must get their OWN identity — never
    take over the therapist's (the consumer-solo behaviour)."""
    sid = _insert_solo_session(therapist_id="therapist-xyz", created_by="therapist-xyz")

    rv = enc_client.post("/session/join", data={"session_id": sid})
    assert rv.status_code == 302
    assert "/auth/solo" in rv.headers["Location"]
    with enc_client.session_transaction() as sess:
        assert sess.get("pending_solo_session") == sid
        assert sess.get("user_id") != "therapist-xyz"


def test_consumer_solo_join_unchanged(enc_client):
    """Regression: joining a normal (AI-led) solo session still resumes the
    creator's identity, exactly as before."""
    sid = _insert_solo_session(therapist_id=None, created_by="owner-abc")

    rv = enc_client.post("/session/join", data={"session_id": sid})
    assert rv.status_code == 302
    assert f"/therapy/solo/{sid}" in rv.headers["Location"]
    with enc_client.session_transaction() as sess:
        assert sess.get("user_id") == "owner-abc"


def test_therapist_led_solo_cards_isolated_and_no_autoreply(enc_client):
    """In a therapist-led 1:1: cards reach only the therapist and the AI never
    replies to the room — same guarantees as couple/group, on solo mode."""
    t_sio, c_sio, sid, client_user = _join_pair(enc_client, mode="solo")

    fake = [{"type": "observation", "text": "Client minimizes their own needs.", "confidence": 0.6}]
    with patch("copilot.generate_suggestions", return_value=fake):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "I guess it's fine, whatever", "mode": "solo"})

    t_recv = t_sio.get_received()
    c_recv = c_sio.get_received()

    assert "suggestion_cards" in _names(t_recv)
    assert "suggestion_cards" not in _names(c_recv)
    c_new = _args_of(c_recv, "new_message")
    assert all(m["user_id"] != "AI" for m in c_new)
