"""
tests/test_session_controls.py
------------------------------
Batch A session controls: 'Hide my data' is client-only; only the clinician can
end a session (clients are notified); only the clinician can set the shared
friendly name (clients get it, late joiners are synced).
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-sc")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")
os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from datetime import datetime, timezone, timedelta

import TogetherMindsAI as tm
from TogetherMindsAI import app, socketio
from models import db, init_encryption, TherapySession, SessionStateCert
from session_id import generate_session_id
from tests.socket_utils import authed_socket, certify_state

init_encryption(os.environ["FIELD_ENCRYPTION_KEY"])


@pytest.fixture
def enc_client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def _seed(therapist="ther-1", mode="group"):
    sid = generate_session_id()
    now = datetime.now(timezone.utc)
    db.session.add(TherapySession(id=sid, mode=mode, created_by=therapist, created_at=now,
                                  retention_expires_at=now + timedelta(days=30), therapist_id=therapist))
    db.session.commit()
    return sid


def _sock(sid, uid, mode="group"):
    """Authenticated socket for `uid` in `sid`. The session's therapist gets a
    clinician session; anyone else gets a consented + licensure-certified client
    session, so identity is real and the admission gate is cleared."""
    ts = db.session.get(TherapySession, sid)
    ther = ts.therapist_id if ts else None
    if uid == ther:
        return authed_socket(app, socketio, uid, clinician=True)
    if ther:
        certify_state(db, SessionStateCert, sid, ther, state="CA")
    return authed_socket(app, socketio, uid, session_id=sid, state="CA")


def _join(client, sid, uid):
    sio = _sock(sid, uid, "group")
    sio.emit("join", {"session_id": sid, "mode": "group"})
    sio.get_received()
    return sio


def _names(sio):
    return [e["name"] for e in sio.get_received()]


# ---- Hide my data (progress page) ----

def test_hide_my_data_hidden_for_clinician(enc_client):
    with enc_client.session_transaction() as s:
        s["user_id"] = "doc"; s["clinician_id"] = "doc"
    rv = enc_client.get("/progress/doc/couple")
    assert rv.status_code == 200
    assert b"Hide my data" not in rv.data


def test_hide_my_data_shown_for_client(enc_client):
    with enc_client.session_transaction() as s:
        s["user_id"] = "cli"
    rv = enc_client.get("/progress/cli/couple")
    assert rv.status_code == 200
    assert b"Hide my data" in rv.data


# ---- Display name: a first-time name is broadcast to others (not just the caller) ----

def test_first_time_name_broadcasts_to_others(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")   # therapist present → real join
    t.get_received(); c.get_received()
    c.emit("set_display_name", {"session_id": sid, "user_id": "client-1", "display_name": "David"})
    # The OTHER participant (therapist) must receive name_changed so their UI relabels.
    assert any(e["name"] == "name_changed" and e["args"][0]["new_name"] == "David"
               for e in t.get_received())
    # The caller still gets name_set (closes their modal / updates their own banner).
    assert any(e["name"] == "name_set" for e in c.get_received())


# ---- End session — over HTTP (reliable, cookie-authenticated; the only end path) ----

def test_end_session_http_notifies_clients_and_emails(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"                       # signed in as the clinician
    with patch.object(tm, "_dispatch_session_transcript") as dispatch:
        rv = enc_client.post(f"/session/{sid}/end")
    assert rv.status_code in (302, 303)               # form post → redirect to dashboard
    dispatch.assert_called_once_with(sid)
    assert "session_ended" in _names(c)               # client notified over its socket


def test_session_ended_page_shows_for_clinician(enc_client):
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "doc"; s["user_id"] = "doc"
    rv = enc_client.get("/session-ended")
    assert rv.status_code == 200
    assert b"Session ended" in rv.data
    assert b"http-equiv=\"refresh\"" in rv.data   # auto-returns, no JS


def test_session_ended_page_requires_clinician(enc_client):
    rv = enc_client.get("/session-ended")
    assert rv.status_code == 302 and "/login" in rv.headers["Location"]


def test_end_session_http_forbidden_for_non_owner(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    # not signed in
    assert enc_client.post(f"/session/{sid}/end").status_code == 403
    # signed in as someone else
    with enc_client.session_transaction() as s:
        s["user_id"] = "intruder"
    assert enc_client.post(f"/session/{sid}/end").status_code == 403


# ---- Waiting room (no client conversation without a clinician present) ----

def _raw_join(client, sid, uid, mode="couple"):
    sio = _sock(sid, uid, mode)
    sio.emit("join", {"session_id": sid, "mode": mode})
    return sio


def test_client_held_in_waiting_room_without_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1", mode="couple")
    c = _raw_join(enc_client, sid, "client-1")
    names = _names(c)
    assert "waiting_room" in names
    assert "history" not in names            # not admitted to the live session


def test_therapist_arrival_admits_waiting_clients(enc_client):
    with app.app_context():
        sid = _seed("ther-1", mode="couple")
    c = _raw_join(enc_client, sid, "client-1"); c.get_received()
    _raw_join(enc_client, sid, "ther-1")     # clinician arrives
    assert "session_open" in _names(c)       # waiting client told to enter


def test_waiting_client_self_recovers_on_rejoin_when_therapist_present(enc_client):
    """A client held in the waiting room polls join with from_waiting; once the
    clinician is present, that poll is answered with session_open (page reloads
    into the live session) — no manual refresh. Covers a deploy/restart that wiped
    in-memory presence."""
    with app.app_context():
        sid = _seed("ther-1", mode="couple")
    c = _raw_join(enc_client, sid, "client-1")
    assert "waiting_room" in _names(c)
    t = _raw_join(enc_client, sid, "ther-1")        # clinician now present
    t.get_received(); c.get_received()              # drain the arrival session_open
    c.emit("join", {"session_id": sid, "user_id": "client-1",
                    "mode": "couple", "from_waiting": True})
    assert "session_open" in _names(c)


def test_normal_join_with_therapist_present_does_not_reload(enc_client):
    """A first-time join (no from_waiting) when the clinician is present is admitted
    normally — no session_open, so the page is not reloaded in a loop."""
    with app.app_context():
        sid = _seed("ther-1", mode="couple")
    t = _raw_join(enc_client, sid, "ther-1"); t.get_received()
    c = _raw_join(enc_client, sid, "client-1")
    names = _names(c)
    assert "waiting_room" not in names
    assert "session_open" not in names


def test_heartbeat_admits_client_without_therapist_socket(enc_client):
    """Presence is the DB heartbeat: an HTTP heartbeat alone (no therapist socket)
    is enough to admit a client — proving presence is decoupled from sockets."""
    with app.app_context():
        sid = _seed("ther-1", mode="group")
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    assert enc_client.post(f"/session/{sid}/heartbeat").status_code == 200
    c = _raw_join(enc_client, sid, "client-1", mode="group")
    assert "waiting_room" not in _names(c)


def test_stale_heartbeat_holds_client(enc_client):
    """An old heartbeat (therapist gone) sends the client back to the waiting room."""
    with app.app_context():
        sid = _seed("ther-1", mode="group")
        ts = db.session.get(TherapySession, sid)
        ts.therapist_last_seen = datetime.now(timezone.utc) - timedelta(seconds=120)
        db.session.commit()
    c = _raw_join(enc_client, sid, "client-1", mode="group")
    assert "waiting_room" in _names(c)


def test_heartbeat_requires_therapist(enc_client):
    with app.app_context():
        sid = _seed("ther-1", mode="group")
    assert enc_client.post(f"/session/{sid}/heartbeat").status_code == 403   # not signed in
    with enc_client.session_transaction() as s:
        s["user_id"] = "intruder"
    assert enc_client.post(f"/session/{sid}/heartbeat").status_code == 403   # wrong user
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    assert enc_client.post(f"/session/{sid}/heartbeat").status_code == 200   # the therapist


def test_client_message_blocked_without_therapist(enc_client):
    from models import ChatMessage
    with app.app_context():
        sid = _seed("ther-1", mode="couple")
    c = _raw_join(enc_client, sid, "client-1"); c.get_received()
    c.emit("send_message", {"session_id": sid, "user_id": "client-1", "text": "anyone?"})
    names = _names(c)
    assert "waiting_room" in names
    assert "new_message" not in names
    with app.app_context():
        assert ChatMessage.query.filter_by(session_id=sid).count() == 0


# ---- Tokenized end-session transcript download ----

def test_session_transcript_token_route(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
        ts = db.session.get(TherapySession, sid); ts.download_token = "stok"; db.session.commit()
    rv = enc_client.get("/session/transcript/stok/pdf")          # not signed in
    assert rv.status_code == 302 and "/login" in rv.headers["Location"]
    with enc_client.session_transaction() as s:
        s["user_id"] = "ther-1"
    assert enc_client.get("/session/transcript/stok/pdf").status_code == 200
    assert enc_client.get("/session/transcript/stok/docx").status_code == 200
    assert enc_client.get("/session/transcript/nope/pdf").status_code == 404
    assert enc_client.get("/session/transcript/stok/txt").status_code == 404


# ---- Friendly name ----

def test_therapist_sets_friendly_name_broadcasts_and_stores(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    t.emit("set_friendly_name", {"session_id": sid, "user_id": "ther-1", "name": "Smith — wk3"})
    fn = [e for e in c.get_received() if e["name"] == "friendly_name_set"]
    assert fn and fn[0]["args"][0]["name"] == "Smith — wk3"
    assert tm.session_friendly_name.get(sid) == "Smith — wk3"


def test_non_therapist_cannot_set_friendly_name(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    tm.session_friendly_name.pop(sid, None)
    c.emit("set_friendly_name", {"session_id": sid, "user_id": "client-1", "name": "hax"})
    assert tm.session_friendly_name.get(sid) is None


def test_join_syncs_existing_friendly_name(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    tm.session_friendly_name[sid] = "Existing"
    _join(enc_client, sid, "ther-1")    # clinician present so the newcomer is admitted
    sio = _sock(sid, "c2", "group")
    sio.emit("join", {"session_id": sid, "mode": "group"})
    fn = [e for e in sio.get_received() if e["name"] == "friendly_name_set"]
    assert fn and fn[0]["args"][0]["name"] == "Existing" and fn[0]["args"][0].get("silent") is True
    tm.session_friendly_name.pop(sid, None)


# ---- Co-pilot chattiness (Batch B) ----

def test_copilot_should_emit_respects_cadence(enc_client):
    sid = "s-cad"
    tm.session_copilot_cadence[sid] = "stop"
    assert tm._copilot_should_emit(sid) is False
    tm.session_copilot_cadence[sid] = "more"
    assert tm._copilot_should_emit(sid) is True
    tm.session_copilot_cadence[sid] = "less"
    tm.session_copilot_emit_counter[sid] = 0
    assert [tm._copilot_should_emit(sid) for _ in range(3)] == [False, False, True]
    tm.session_copilot_cadence.pop(sid, None)
    tm.session_copilot_emit_counter.pop(sid, None)


def test_copilot_cadence_socket_therapist_only(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    tm.session_copilot_cadence.pop(sid, None)
    c.emit("copilot_cadence", {"session_id": sid, "user_id": "client-1", "mode": "stop"})
    assert tm.session_copilot_cadence.get(sid) is None        # client ignored
    t.emit("copilot_cadence", {"session_id": sid, "user_id": "ther-1", "mode": "stop"})
    assert tm.session_copilot_cadence.get(sid) == "stop"      # therapist applies
    tm.session_copilot_cadence.pop(sid, None)


def test_session_copilot_cards_returns_persisted(enc_client):
    with app.app_context():
        sid = _seed("ther-1")
        tm._persist_cards(sid, [{"type": "risk", "text": "Risk note", "code": ""},
                                {"type": "reference", "text": "Ref", "code": "F32"}], "ther-1")
        cards = tm._session_copilot_cards(sid)
    assert len(cards) == 2
    assert cards[0]["type"] == "risk" and cards[1]["code"] == "F32"


def test_run_copilot_saves_even_when_stopped(enc_client):
    from models import CopilotCard
    with app.app_context():
        sid = _seed("ther-1")
        tm.session_copilot_cadence[sid] = "stop"
        with patch("copilot.build_risk_cards", return_value=[{"type": "risk", "text": "R", "confidence": 0.9}]), \
             patch("copilot.build_reference_cards", return_value=[]), \
             patch("copilot.generate_suggestions", return_value=[]), \
             patch("copilot.dedupe_cards", side_effect=lambda c, r: c), \
             patch.object(tm.socketio, "emit") as emit_mock:
            tm._run_copilot(sid, "group", trigger_text="hello", trigger_user_id="client-1")
        n = CopilotCard.query.filter_by(session_id=sid).count()
    assert n == 1                                            # saved to record despite "stop"
    emitted = [c for c in emit_mock.call_args_list if c.args and c.args[0] == "suggestion_cards"]
    assert not emitted                                      # but NOT shown live
    tm.session_copilot_cadence.pop(sid, None)


# ---- Friendly name: unique, persisted, joinable (Batch D) ----

def test_friendly_name_persisted_unique_with_suggestion(enc_client):
    with app.app_context():
        sid1 = _seed("ther-1"); sid2 = _seed("ther-2")
    t1 = _join(enc_client, sid1, "ther-1"); t1.get_received()
    t1.emit("set_friendly_name", {"session_id": sid1, "user_id": "ther-1", "name": "CoupleTest"})
    assert any(e["name"] == "friendly_name_set" and e["args"][0]["name"] == "CoupleTest"
               for e in t1.get_received())
    with app.app_context():
        assert db.session.get(TherapySession, sid1).friendly_name == "CoupleTest"
    # A different session can't reuse it — gets a 'taken' + suggestion, and is NOT applied.
    t2 = _join(enc_client, sid2, "ther-2"); t2.get_received()
    t2.emit("set_friendly_name", {"session_id": sid2, "user_id": "ther-2", "name": "CoupleTest"})
    taken = [e for e in t2.get_received() if e["name"] == "friendly_name_taken"]
    assert taken and taken[0]["args"][0]["suggestion"] == "CoupleTest1"
    with app.app_context():
        assert db.session.get(TherapySession, sid2).friendly_name is None


def test_join_by_friendly_name_or_combined(enc_client):
    with app.app_context():
        from models import friendly_name_key
        sid = _seed("ther-1", mode="couple")
        ts = db.session.get(TherapySession, sid)
        ts.friendly_name = "MyName"; ts.friendly_name_key = friendly_name_key("MyName")
        db.session.commit()
    # by friendly name alone, by combined "ID-name", and a bad one
    assert enc_client.post("/session/join", data={"session_id": "MyName"}).status_code in (302, 303)
    assert enc_client.post("/session/join", data={"session_id": sid + "-MyName"}).status_code in (302, 303)
    bad = enc_client.post("/session/join", data={"session_id": "NoSuchName"})
    assert bad.status_code == 200 and b"not found" in bad.data.lower()


def test_display_name_persisted_and_restored(enc_client):
    from models import SessionParticipant
    with app.app_context():
        sid = _seed("ther-1")
        tm._claim_display_name(sid, "u1", "David")
        row = SessionParticipant.query.filter_by(session_id=sid, user_id="u1").first()
        assert row and row.display_name == "David"
        # Simulate a restart: drop the in-memory maps, then restore from the DB.
        tm.session_display_names.pop(sid, None)
        tm.session_taken_names.pop(sid, None)
        tm._restore_display_names(sid)
        assert tm.session_display_names.get(sid, {}).get("u1") == "David"
        tm.session_display_names.pop(sid, None)
        tm.session_taken_names.pop(sid, None)


# ---- Join consent acknowledgement (client only) → transcript + Co-Pilot ----

def test_consent_records_transcript_and_copilot_card(enc_client):
    """A client agrees on the dedicated consent gate (HTTP POST, before the room
    loads). The agreement is recorded in the transcript, surfaced live in the
    therapist's Co-Pilot (never the client's), and persisted to the card history."""
    from models import ChatMessage, CopilotCard
    with app.app_context():
        sid = _seed("ther-1")
    t = _join(enc_client, sid, "ther-1")
    c = _join(enc_client, sid, "client-1")
    t.get_received(); c.get_received()
    c.emit("set_display_name", {"session_id": sid, "user_id": "client-1", "display_name": "David"})
    t.get_received(); c.get_received()

    # The client passes the consent gate before entering the room.
    with enc_client.session_transaction() as sess:
        sess["user_id"] = "client-1"
    resp = enc_client.post(f"/session/{sid}/consent",
                           data={"state": "NY", "location_attest": "1"})
    assert resp.status_code in (301, 302)   # held at the state gate (consent still recorded)

    # 1) The therapist sees an informational Co-Pilot line; the client never does.
    t_cards = [e for e in t.get_received() if e["name"] == "suggestion_cards"]
    assert t_cards and "agreed to the recording & transcription consent" in \
        t_cards[0]["args"][0]["cards"][0]["text"]
    assert "suggestion_cards" not in _names(c)

    with app.app_context():
        # 2) Recorded in the transcript, attributed to the client.
        msg = ChatMessage.query.filter_by(session_id=sid, user_id="client-1").first()
        assert msg and msg.text.startswith("[Consent]")
        # 3) Persisted to the card history so it survives a therapist reconnect.
        assert CopilotCard.query.filter_by(session_id=sid).count() == 1


def test_consent_from_therapist_is_ignored(enc_client):
    """The therapist leads the session and is never consent-gated; a consent POST
    attributed to them records nothing."""
    from models import ChatMessage
    with app.app_context():
        sid = _seed("ther-1")
    with enc_client.session_transaction() as sess:
        sess["user_id"] = "ther-1"
    enc_client.post(f"/session/{sid}/consent")
    with app.app_context():
        assert ChatMessage.query.filter_by(session_id=sid).count() == 0


# ---- Graceful leave: session page (A + C) ----

def test_session_room_has_leave_modal_and_newtab_links(enc_client):
    """In a live session: Home/Sign out get a confirm modal (A); My sessions +
    footer legal links open in a new tab (C)."""
    with app.app_context():
        sid = _seed("ther-1", mode="solo")
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "ther-1"; s["user_id"] = "ther-1"
    html = enc_client.get(f"/therapy/solo/{sid}").get_data(as_text=True)
    assert 'id="leaveSessionModal"' in html          # A: Home/Sign out confirm
    assert 'id="contentWindow"' in html              # C: My sessions/legal open in a floating window
    assert 'id="contentFrame"' in html               # the window's embed iframe
    assert 'floating-window.js' in html              # drag/resize controller loaded


def test_non_session_page_has_no_leave_ux(enc_client):
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "ther-1"; s["user_id"] = "ther-1"
    html = enc_client.get("/therapist").get_data(as_text=True)
    assert 'id="leaveSessionModal"' not in html
    assert 'id="contentWindow"' not in html


def test_embed_pages_strip_chrome(enc_client):
    """?embed=1 renders the page for the modal iframe: no navbar, but full content."""
    full = enc_client.get("/privacy").get_data(as_text=True)
    embed = enc_client.get("/privacy?embed=1").get_data(as_text=True)
    assert "<nav " in full and "<nav " not in embed        # chrome stripped in embed
    assert "Privacy" in embed                               # content still rendered
    assert "<nav " not in enc_client.get("/tos?embed=1").get_data(as_text=True)


def test_in_session_nav_hides_home_and_my_sessions(enc_client):
    """In a live session the nav is focused: Home + My/Your sessions are hidden;
    the account menu (Sign out) stays. The dashboard still shows them."""
    with app.app_context():
        sid = _seed("ther-1", mode="solo")
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "ther-1"; s["user_id"] = "ther-1"
    html = enc_client.get(f"/therapy/solo/{sid}").get_data(as_text=True)
    assert "bi-house-fill" not in html           # Home nav hidden in-session
    assert "bi-clipboard2-pulse" not in html     # My sessions nav hidden in-session
    assert "/logout" in html                      # account menu / Sign out remains
    # The dashboard (not in-session) still shows the sessions nav.
    assert "bi-clipboard2-pulse" in enc_client.get("/therapist").get_data(as_text=True)


def test_pricing_link_is_in_the_navbar_for_clinicians(enc_client):
    """It was only reachable from a link at the very bottom of the dashboard, and
    not at all from the home page. It belongs in the masthead — and it keeps the
    position it had when it read "Plans & billing"."""
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "ther-1"; s["user_id"] = "ther-1"
    html = enc_client.get("/therapist").get_data(as_text=True)
    assert "Pricing" in html
    assert "bi-credit-card" in html
    # Still sits between My sessions and the account menu.
    assert html.index("bi-clipboard2-pulse") < html.index("bi-credit-card") < html.index("acctMenu")


def test_pricing_link_is_shown_to_a_signed_out_visitor(enc_client):
    """This test used to assert the OPPOSITE — that the link was hidden from
    anyone not signed in, on the reasoning that subscriptions are per clinician.
    That reasoning was wrong: it meant nobody could find out what the software
    cost without signing up first. The rule is now that a price is public.
    """
    html = enc_client.get("/welcome").get_data(as_text=True)
    assert "bi-credit-card" in html
    assert "Pricing" in html


def test_billing_link_hidden_during_a_live_session(enc_client):
    """The in-session nav stays focused; billing goes with the rest of it."""
    with app.app_context():
        sid = _seed("ther-1", mode="solo")
    with enc_client.session_transaction() as s:
        s["clinician_id"] = "ther-1"; s["user_id"] = "ther-1"
    html = enc_client.get(f"/therapy/solo/{sid}").get_data(as_text=True)
    assert "bi-credit-card" not in html
