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


def _insert_session(mode="couple", therapist_id=None, created_by="owner-xyz"):
    """Insert a TherapySession directly. Sessions are created server-side only by a
    logged-in clinician (POST /therapist/start/<mode>); tests build them here."""
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by=created_by,
        created_at=datetime.now(timezone.utc),
        retention_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        therapist_id=therapist_id,
    ))
    db.session.commit()
    return sid


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
# ICD grounding — reference block injection + grounded cards
# ---------------------------------------------------------------------------

def test_generate_suggestions_injects_reference_block_when_matched():
    """A clinically-loaded transcript injects an ICD reference block into the prompt
    so the model's own cards are grounded in the curated corpus."""
    cl = _claude_returning("[]")
    with patch("copilot._get_claude_client", return_value=cl):
        copilot.generate_suggestions("Client: I'm anxious and worry all the time", mode="solo")
    user_msg = cl.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Reference material" in user_msg
    assert "F41.1" in user_msg                       # GAD ICD-10 code reached the prompt


def test_generate_suggestions_no_reference_block_when_benign():
    cl = _claude_returning("[]")
    with patch("copilot._get_claude_client", return_value=cl):
        copilot.generate_suggestions("Client: thanks, see you next week", mode="solo")
    user_msg = cl.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Reference material" not in user_msg


def test_build_reference_cards_reexported_from_copilot():
    cards = copilot.build_reference_cards("I keep having flashbacks and nightmares, feeling triggered")
    assert cards and cards[0]["type"] == "reference"
    assert "F43.10" in cards[0]["code"]


def test_build_reference_cards_grounds_plain_financial_stress():
    """Everyday financial/employment-stress language (no clinical jargon) now clears
    the threshold and maps to Adjustment disorder — closing the gap where a real
    'I'm stressed, I lost my job, can't pay rent' session grounded to nothing."""
    cards = copilot.build_reference_cards(
        "I'm so stressed out — I lost my job and now I can't pay the rent"
    )
    assert cards
    assert any("F43.2" in c.get("code", "") for c in cards)


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
    therapist_id = str(uuid.uuid4())
    sid = _insert_session(mode=mode, therapist_id=therapist_id, created_by=therapist_id)
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


def test_reference_card_reaches_only_therapist(enc_client):
    """A clinically-loaded client message produces a grounded ICD reference card,
    built deterministically from the corpus, delivered only to the therapist."""
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    # generate_suggestions is stubbed to [] so the only card is the grounded one.
    with patch("copilot.generate_suggestions", return_value=[]):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "I keep having flashbacks and nightmares, I feel triggered",
                                    "mode": "couple"})

    t_cards = _args_of(t_sio.get_received(), "suggestion_cards")
    assert t_cards
    ref = [c for c in t_cards[0]["cards"] if c["type"] == "reference"]
    assert ref and "F43.10" in ref[0]["code"]
    assert "suggestion_cards" not in _names(c_sio.get_received())


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


def test_therapist_own_message_triggers_copilot(enc_client):
    """The therapist's own messages drive the co-pilot too — feedback on their
    interventions, not just the client's words — and stay private to the therapist."""
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)
    therapist_id = session_therapist_id[sid]

    fake = [{"type": "observation", "text": "Reframe may move too fast — check it landed.", "confidence": 0.6}]
    with patch("copilot.generate_suggestions", return_value=fake):
        t_sio.emit("send_message", {"session_id": sid, "user_id": therapist_id,
                                    "text": "It sounds like you're being hard on yourself.",
                                    "mode": "couple"})

    assert "suggestion_cards" in _names(t_sio.get_received())
    assert "suggestion_cards" not in _names(c_sio.get_received())


def test_therapist_note_rejected_from_non_therapist(enc_client):
    t_sio, c_sio, sid, client_user = _join_pair(enc_client)

    with patch("copilot.generate_suggestions", return_value=[{"type": "question",
               "text": "x", "confidence": 0.5}]) as gen:
        c_sio.emit("therapist_note", {"session_id": sid, "user_id": client_user,
                                      "text": "I am pretending to be the therapist"})
        gen.assert_not_called()
    assert "suggestion_cards" not in _names(t_sio.get_received())


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


def test_consumer_solo_join_rejected(enc_client):
    """Solo is therapist-led only: a solo session with no therapist_id can no
    longer be created, so joining one is rejected (no creator-identity resume)."""
    sid = _insert_solo_session(therapist_id=None, created_by="owner-abc")

    rv = enc_client.post("/session/join", data={"session_id": sid})
    # Rendered "Session not found" page, not a redirect into the room.
    assert rv.status_code == 200
    assert b"Session not found" in rv.data
    with enc_client.session_transaction() as sess:
        assert sess.get("user_id") != "owner-abc"


def _insert_couple_session(therapist_id=None, created_by="owner-xyz"):
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode="couple", created_by=created_by,
        created_at=datetime.now(timezone.utc),
        retention_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        therapist_id=therapist_id,
    ))
    db.session.commit()
    return sid


def test_therapist_led_couple_page_gets_polish_class(enc_client):
    """A therapist-led couple page carries the scoped polish class."""
    sid = _insert_couple_session(therapist_id="ther-1", created_by="ther-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    rv = enc_client.get("/therapy/couple/" + sid)
    assert rv.status_code == 200
    assert b"tcp-session" in rv.data


def test_consumer_couple_page_unstyled(enc_client):
    """Regression: a normal (consumer) couple page must NOT get the polish class —
    the therapist-led styling never bleeds into the regular experience."""
    sid = _insert_couple_session(therapist_id=None, created_by="owner-1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "owner-1"
        s["consented_sessions"] = [sid]   # past the consent gate
    rv = enc_client.get("/therapy/couple/" + sid)
    assert rv.status_code == 200
    assert b"tcp-session" not in rv.data


def test_therapist_led_composer_is_enabled(enc_client):
    """Regression: therapist-led pages must NOT disable the send button — there is
    no AI opening message to unlock it, so a hard-disabled composer = unusable."""
    # solo room (always therapist-led)
    s_sid = _insert_solo_session(therapist_id="t1", created_by="t1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "t1"
    s_body = enc_client.get("/therapy/solo/" + s_sid).get_data(as_text=True)
    assert 'id="sendBtn"' in s_body
    assert 'id="sendBtn" disabled' not in s_body

    # therapist-led couple
    c_sid = _insert_couple_session(therapist_id="t2", created_by="t2")
    with enc_client.session_transaction() as s:
        s["user_id"] = "t2"
    c_body = enc_client.get("/therapy/couple/" + c_sid).get_data(as_text=True)
    assert 'id="sendBtn" disabled' not in c_body


def test_consumer_couple_composer_stays_gated(enc_client):
    """Consumer couple keeps the disabled-until-AI-opens composer (unchanged)."""
    sid = _insert_couple_session(therapist_id=None, created_by="o1")
    with enc_client.session_transaction() as s:
        s["user_id"] = "o1"
        s["consented_sessions"] = [sid]   # past the consent gate
    body = enc_client.get("/therapy/couple/" + sid).get_data(as_text=True)
    assert 'id="sendBtn" disabled' in body


# ---------------------------------------------------------------------------
# Unified session room — one template/handler for solo / couple / group
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,label,placeholder", [
    ("solo",   "1:1 Session",     "Type a message"),
    ("couple", "Couple Check-in", "Type a message"),
    ("group",  "Group Circle",    "Share with the group"),
])
def test_each_mode_renders_its_config(enc_client, mode, label, placeholder):
    """The unified room renders each mode's own label, placeholder, and MODE var —
    so the correct mode reaches the client (and therefore the co-pilot's framing)."""
    tid = "t-" + mode
    sid = _insert_session(mode=mode, therapist_id=tid, created_by=tid)
    with enc_client.session_transaction() as s:
        s["user_id"] = tid
    body = enc_client.get(f"/therapy/{mode}/{sid}").get_data(as_text=True)
    assert label in body
    assert placeholder in body
    assert f'"{mode}"' in body                       # var MODE / window.SESSION_MODE
    # Progress now opens in a modal (iframe → embedded progress page) rather than
    # navigating away, so the button targets the modal and the embed URL is built
    # in JS from USER_ID/MODE (which still carry the right therapist id + mode).
    assert 'data-bs-target="#progressModal"' in body
    assert "?embed=1" in body
    assert f'"{tid}"' in body                         # USER_ID var carries the therapist id


def test_url_mode_mismatch_redirects_to_canonical(enc_client):
    """The stored mode is authoritative: hitting the wrong mode's URL redirects to
    the right room, so a couple session is never shown (or advised on) as a group."""
    sid = _insert_session(mode="group", therapist_id="tg", created_by="tg")
    with enc_client.session_transaction() as s:
        s["user_id"] = "tg"
    rv = enc_client.get(f"/therapy/couple/{sid}")
    assert rv.status_code == 302
    assert rv.headers["Location"].endswith(f"/therapy/group/{sid}")


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


def test_therapist_default_display_name_is_therapist(enc_client):
    """The clinician's default name is 'Therapist' in every mode, so their role is clear."""
    for mode in ("solo", "couple", "group"):
        therapist_id = str(uuid.uuid4())
        sid = _insert_session(mode=mode, therapist_id=therapist_id, created_by=therapist_id)
        t_sio = socketio.test_client(app, flask_test_client=enc_client)
        t_sio.emit("join", {"session_id": sid, "user_id": therapist_id, "mode": mode})
        hist = _args_of(t_sio.get_received(), "history")
        assert hist and hist[0]["default_name"] == "Therapist", mode


def test_client_default_name_keeps_mode_prefix(enc_client):
    """A client (not the therapist) still gets the mode-based default name."""
    therapist_id, client_user = str(uuid.uuid4()), str(uuid.uuid4())
    sid = _insert_session(mode="group", therapist_id=therapist_id, created_by=therapist_id)
    t_sio = socketio.test_client(app, flask_test_client=enc_client)
    t_sio.emit("join", {"session_id": sid, "user_id": therapist_id, "mode": "group"})
    t_sio.get_received()
    c_sio = socketio.test_client(app, flask_test_client=enc_client)
    c_sio.emit("join", {"session_id": sid, "user_id": client_user, "mode": "group"})
    hist = _args_of(c_sio.get_received(), "history")
    assert hist and hist[0]["default_name"].startswith("GroupMember")


# ---------------------------------------------------------------------------
# Card persistence + history replay (cards survive reload / restart)
# ---------------------------------------------------------------------------

def test_cards_are_persisted_for_retrieval(enc_client):
    """Emitted co-pilot cards are stored, so they can be replayed later instead of
    being lost once they scroll out of the live console."""
    from models import CopilotCard

    t_sio, c_sio, sid, client_user = _join_pair(enc_client)
    fake = [{"type": "question", "text": "Ask what changed this week.", "confidence": 0.7}]
    with patch("copilot.generate_suggestions", return_value=fake):
        c_sio.emit("send_message", {"session_id": sid, "user_id": client_user,
                                    "text": "Things feel different lately", "mode": "couple"})

    with app.app_context():
        rows = CopilotCard.query.filter_by(session_id=sid).all()
        assert any(r.card_type == "question" and "changed this week" in r.text for r in rows)
        # The triggering speaker is recorded.
        assert any(r.trigger_user_id == client_user for r in rows)


def test_therapist_join_replays_card_history(enc_client):
    """A therapist (re)joining receives the stored cards as `card_history`, so the
    panel shows full history rather than starting blank."""
    from models import CopilotCard

    therapist_id = str(uuid.uuid4())
    sid = _insert_session(mode="couple", therapist_id=therapist_id, created_by=therapist_id)
    with app.app_context():
        card = {"type": "observation", "text": "Earlier note worth recalling.", "confidence": 0.5}
        db.session.add(CopilotCard(
            session_id=sid, card_type="observation",
            text=card["text"], payload=json.dumps(card), confidence=0.5,
        ))
        db.session.commit()

    t_sio = socketio.test_client(app, flask_test_client=enc_client)
    t_sio.emit("join", {"session_id": sid, "user_id": therapist_id, "mode": "couple"})
    hist = _args_of(t_sio.get_received(), "card_history")
    assert hist and any("Earlier note" in c["text"] for c in hist[0]["cards"])


def test_card_history_not_sent_to_clients(enc_client):
    """History replay is therapist-only — a client joining must never receive it."""
    from models import CopilotCard

    therapist_id = str(uuid.uuid4())
    sid = _insert_session(mode="couple", therapist_id=therapist_id, created_by=therapist_id)
    with app.app_context():
        card = {"type": "observation", "text": "Private earlier note.", "confidence": 0.5}
        db.session.add(CopilotCard(
            session_id=sid, card_type="observation",
            text=card["text"], payload=json.dumps(card), confidence=0.5,
        ))
        db.session.commit()

    c_sio = socketio.test_client(app, flask_test_client=enc_client)
    c_sio.emit("join", {"session_id": sid, "user_id": str(uuid.uuid4()), "mode": "couple"})
    assert "card_history" not in _names(c_sio.get_received())
