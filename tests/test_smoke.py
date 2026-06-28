"""
Smoke tests — verify the app starts, all key pages return correct status codes,
and the full golden-path (register → challenge → verify → send message) works
end-to-end against a real in-process server.

These tests exercise the production code path including eventlet/SocketIO setup,
database schema, and HTTP routing — things unit tests with mocked internals cannot catch.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import base64
import sqlite3
import uuid
import pytest

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-smoke")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, ECDSA, SECP256R1
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from datetime import datetime, timezone
from TogetherMindsAI import app
from models import db, User, TherapySession, ChatMessage
from session_id import generate_session_id, is_valid_session_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keypair():
    priv = generate_private_key(SECP256R1())
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    return priv, pub_b64


def _sign(priv, message: str) -> str:
    return base64.b64encode(priv.sign(message.encode(), ECDSA(SHA256()))).decode()


def _register(client, mode="solo"):
    """Create a non-therapist-led (AI-led) session and register an anonymous client
    joining it — registration is join-only now, so the session is built here. The
    smoke tests exercise the AI-led path, so therapist_id stays None.
    Returns (priv_key, user_id, session_id)."""
    priv, pub_b64 = _make_keypair()
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode=mode, created_by="pending",
        created_at=datetime.now(timezone.utc),
    ))
    db.session.commit()
    with client.session_transaction() as s:
        s[f"pending_{mode}_session"] = sid
    rv = client.post("/api/auth/register",
                     json={"public_key": pub_b64, "therapy_mode": mode})
    assert rv.status_code == 201, rv.get_data(as_text=True)
    data = rv.get_json()
    user_id = data["user_id"]
    # Match the old behaviour where the registering user created the session.
    ts = db.session.get(TherapySession, sid)
    ts.created_by = user_id
    db.session.commit()
    return priv, user_id, sid


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    # Flask-SQLAlchemy 3.x caches the engine — changing app.config["SQLALCHEMY_DATABASE_URI"]
    # after init_app() has no effect. Override the cached engine directly so that
    # db.create_all() / db.drop_all() operate on an isolated in-memory DB, not the
    # production file.
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


# ---------------------------------------------------------------------------
# Page availability — every route returns the expected status code
# ---------------------------------------------------------------------------

def test_root_redirects_to_welcome(client):
    """First-touch URL `/` must redirect to the welcome/landing page so new
    visitors see the pitch (anonymous, free, three modes) before the mode picker."""
    rv = client.get("/")
    assert rv.status_code in (301, 302)
    assert "/welcome" in rv.headers.get("Location", "")


def test_welcome_page_is_clinician_first(client):
    """The welcome page is therapist-first: it leads with the Co-Pilot CTA to
    /therapist and the session-join action, and no longer surfaces the consumer
    'anonymous / free' self-guided modes."""
    rv = client.get("/welcome")
    assert rv.status_code == 200
    body = rv.data.decode()
    assert "TogetherMindsAI" in body
    # Clinician CTA is the primary action.
    assert 'href="/therapist"' in body
    assert "Start as a clinician" in body
    # Join action for invited clients / returning users.
    assert 'href="/session/join"' in body
    # The consumer 'anonymous / free / self-guided modes' framing is gone.
    assert "100% Anonymous" not in body
    assert 'href="/auth/solo"' not in body


def test_welcome_page_copy_is_audience_neutral_not_clinician_addressed(client):
    """The /welcome page is public — both clients and clinicians land on it.
    Clinician-private framing ('only you see', 'Clients never see it', the AI
    watching the client for 'Live risk alerts') was confusing and unsettling to
    clients reading it. That detail now lives behind the clinician sign-in page;
    the public page speaks in neutral third person. This locks in the split."""
    rv = client.get("/welcome")
    assert rv.status_code == 200
    body = rv.data.decode()
    # Clinician-private phrasing must NOT appear on the public landing page.
    assert "only you see" not in body
    assert "Clients never see it" not in body
    assert "Live risk alerts" not in body
    # The clinician-private detail must live on the sign-in page instead.
    login = client.get("/login")
    assert login.status_code == 200
    login_body = login.data.decode()
    assert "Clients never see it" in login_body


def test_welcome_hero_icon_does_not_collide_with_global_hero_icon(client):
    """Regression: the welcome page's hero icon must not use the bare class
    `hero-icon`. The global rule `.hero-icon` in static/css/style.css paints
    a 90x90 pale-green circle (it was written for the wrapper-div pattern on
    auth/join/progress pages). Applying it to an <i> in the welcome hero drew
    a ghost circle on top of the icon, looking like a broken image. The hero
    must use page-scoped classes only.

    The therapist-first hero uses a page-scoped clipboard-pulse icon."""
    rv = client.get("/welcome")
    assert rv.status_code == 200
    body = rv.data.decode()
    assert "bi-clipboard2-pulse-fill" in body, (
        "Welcome hero must render the clinician clipboard-pulse icon"
    )
    # Lock out the original bug — no bare global hero-icon class on hero icons.
    assert 'bi-heart-pulse-fill hero-icon' not in body
    assert 'bi-heart-fill hero-icon' not in body
    assert 'bi-activity hero-icon' not in body


def test_privacy_and_terms_linked_on_every_page(client):
    """The footer (shared by every page via base.html) must link to BOTH the
    Privacy Policy and the Terms of Service, for all roles. We state this publicly
    ('linked on every page'), so verify it on a public page and on the in-session
    page (client view). Regression guard against the ToS link being dropped."""
    pages = [
        client.get("/welcome").data,
        _session_render(client, as_therapist=False, consented=True).data,  # client session view
    ]
    for body in pages:
        assert b'href="/privacy"' in body, "Privacy Policy link missing from a page footer"
        assert b'href="/tos"' in body, "Terms of Service link missing from a page footer"


def test_billing_page_is_public(client):
    """The pricing page must be viewable WITHOUT signing in (it's public pricing).
    Anonymous visitors see the plans and prices; the 'Choose' buttons point them to
    clinician sign-in rather than 403-ing on checkout."""
    rv = client.get("/billing")
    assert rv.status_code == 200, "GET /billing must not redirect to login"
    body = rv.data
    assert b"$10" in body and b"$25" in body          # Pro + Premium prices
    assert b"Choose Pro" in body and b"Choose Premium" in body
    # Anonymous 'Choose' buttons lead to clinician sign-in, not the checkout POST.
    assert b'href="/login?next=' in body


def test_nda_page_is_locked_by_default(client):
    """The /nda page is password-gated: an unauthenticated visitor sees the
    password prompt, NOT the agreement text."""
    rv = client.get("/nda")
    assert rv.status_code == 200
    assert b"View document" in rv.data                  # unlock form present
    assert b"NON-DISCLOSURE AGREEMENT" not in rv.data    # content hidden


def test_nda_unlocks_only_with_correct_password(client):
    from unittest.mock import patch
    import config
    with patch.object(config, "NDA_SECRET", "open-sesame"):
        # wrong password -> still locked
        client.post("/nda", data={"password": "wrong"})
        assert b"NON-DISCLOSURE AGREEMENT" not in client.get("/nda").data
        # correct password -> unlocked, shows the agreement read live from NDA.docx
        client.post("/nda", data={"password": "open-sesame"})
        rv = client.get("/nda")
        assert rv.status_code == 200
        assert b"NON-DISCLOSURE AGREEMENT" in rv.data
        assert b"Global Business Consulting" in rv.data
        assert b"New York" in rv.data        # the governing-law value edited into the doc


def test_nda_fails_closed_when_secret_unset(client):
    """With no NDA_SECRET configured the page never unlocks (even with a blank
    password), so it can't be left accidentally open."""
    from unittest.mock import patch
    import config
    with patch.object(config, "NDA_SECRET", ""):
        client.post("/nda", data={"password": ""})
        assert b"NON-DISCLOSURE AGREEMENT" not in client.get("/nda").data


def test_home_route_removed_returns_404(client):
    """Regression: /home was deleted after the welcome page absorbed the
    mode-picker UI (the three modes are now clickable directly on /welcome).
    Re-introducing /home would silently restore a duplicate mode picker —
    this test locks in the removal."""
    assert client.get("/home").status_code == 404


def test_navbar_home_link_points_to_root(client):
    """Regression: navbar Home now points to / (which redirects to /welcome)
    since the standalone /home mode-picker page was deleted. Re-pointing the
    nav link at a non-existent /home would cause a 404 when users click it."""
    rv = client.get("/welcome")
    assert rv.status_code == 200
    body = rv.data.decode()
    assert 'href="/home"' not in body, (
        "Navbar must not link to /home — that route was removed"
    )
    # The nav link in base.html should now point at /
    assert '<a class="nav-link" href="/"' in body, (
        "Navbar Home link must target /"
    )


def test_tos_page_returns_200(client):
    rv = client.get("/tos")
    assert rv.status_code == 200
    assert b"Terms of Service" in rv.data
    assert b"togethermindsai@onlinegbc.com" in rv.data
    assert b"findahelpline.com" in rv.data


def test_auth_page_redirects_to_join_without_pending(client):
    # /auth/<mode> is only reachable mid-join now; without a pending session it
    # bounces to the join page (there is no self-directed start).
    for mode in ("solo", "couple", "group"):
        rv = client.get(f"/auth/{mode}")
        assert rv.status_code == 302
        assert "/session/join" in rv.headers["Location"]


def test_session_join_page_returns_200(client):
    assert client.get("/session/join").status_code == 200


def test_progress_page_returns_200(client):
    with app.app_context():
        user_id = str(uuid.uuid4())
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.commit()
    # The progress page is now gated to its own user (see IDOR fix).
    with client.session_transaction() as s:
        s["user_id"] = user_id
    assert client.get(f"/progress/{user_id}/solo").status_code == 200


def test_privacy_page_has_ai_transparency_and_subprocessors(client):
    body = client.get("/privacy").get_data(as_text=True)
    assert "About the AI" in body                 # AIA transparency section
    assert "AssemblyAI" in body                    # sub-processor disclosed
    assert "sub-processor" in body.lower()


def test_privacy_page_discloses_recording_30day_deletion(client):
    body = client.get("/privacy").get_data(as_text=True).lower()
    assert "30 days" in body and "permanently deleted" in body


# ---------------------------------------------------------------------------
# DB schema — all expected columns are present
# ---------------------------------------------------------------------------

def test_users_table_has_auth_columns(client):
    with app.app_context():
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        col_names = {c["name"] for c in inspector.get_columns("users")}
    expected = {"id", "therapy_mode", "public_key", "challenge", "challenge_expires_at"}
    assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"


def test_rate_limit_entries_table_exists(client):
    with app.app_context():
        from sqlalchemy import inspect
        tables = inspect(db.engine).get_table_names()
    assert "rate_limit_entries" in tables


def test_therapy_sessions_table_exists(client):
    with app.app_context():
        from sqlalchemy import inspect
        tables = inspect(db.engine).get_table_names()
    assert "therapy_sessions" in tables


def test_exercises_table_has_mode_column(client):
    with app.app_context():
        from sqlalchemy import inspect
        col_names = {c["name"] for c in inspect(db.engine).get_columns("exercises")}
    assert "mode" in col_names


# ---------------------------------------------------------------------------
# GDPR delete
# ---------------------------------------------------------------------------

def test_delete_user_removes_all_data(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "Hello"})

    rv = client.delete(f"/user/{user_id}")
    assert rv.status_code == 200

    with app.app_context():
        assert db.session.get(User, user_id) is None
        from models import Exercise, ChatMessage
        assert Exercise.query.filter_by(user_id=user_id).count() == 0
        assert ChatMessage.query.filter_by(user_id=user_id).count() == 0


def test_delete_user_forbidden_for_wrong_session(client):
    with app.app_context():
        other_id = str(uuid.uuid4())
        db.session.add(User(id=other_id, therapy_mode="solo"))
        db.session.commit()
    # Client has no session set — should get 403
    rv = client.delete(f"/user/{other_id}")
    assert rv.status_code == 403


# ---------------------------------------------------------------------------
# Input validation — boundary conditions
# ---------------------------------------------------------------------------

def test_nickname_route_removed(client):
    """The server-side nickname route must no longer exist (friendly names are local-only)."""
    priv, user_id, session_id = _register(client, "solo")
    rv = client.post(f"/session/{session_id}/nickname",
                     json={"nickname": "My Monday session"},
                     content_type="application/json")
    assert rv.status_code == 404, (
        f"Expected 404 — nickname route should be removed, got {rv.status_code}"
    )


def test_session_join_unknown_id_shows_error(client):
    rv = client.post("/session/join", data={"session_id": "9999"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()


# ---------------------------------------------------------------------------
# End Session guard — modal and beforeunload wiring
# ---------------------------------------------------------------------------

def test_couple_page_has_end_session_button(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="couple"))
        db.session.add(TherapySession(
            id=session_id, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["consented_sessions"] = [session_id]   # past the consent gate
    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


def test_group_page_has_end_session_button(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="group"))
        db.session.add(TherapySession(
            id=session_id, mode="group", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["consented_sessions"] = [session_id]   # past the consent gate
    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


# ---------------------------------------------------------------------------
# Join/rejoin heading + per-client consent modal + transcription/recording status
# ---------------------------------------------------------------------------

def test_join_page_says_join_or_rejoin(client):
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert b"Join or Rejoin a Session" in rv.data


def _session_render(client, mode="group", as_therapist=False, consented=True):
    """Render the session room. as_therapist=True makes therapist_id == user_id.

    A CLIENT must pass the consent gate before the room loads; consented=True
    pre-marks consent so room-content tests render the room directly. Pass
    consented=False to exercise the gate itself."""
    user_id = str(uuid.uuid4())
    session_id = generate_session_id()
    with app.app_context():
        db.session.add(User(id=user_id, therapy_mode=mode))
        db.session.add(TherapySession(
            id=session_id, mode=mode, created_by=user_id,
            created_at=datetime.now(timezone.utc),
            therapist_id=(user_id if as_therapist else None),
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        if consented and not as_therapist:
            sess["consented_sessions"] = [session_id]
    return client.get(f"/therapy/{mode}/{session_id}")


def _make_client_session(client, mode="group"):
    """Create a session and sign in a CLIENT (no consent yet). Returns the ids."""
    user_id = str(uuid.uuid4())
    session_id = generate_session_id()
    with app.app_context():
        db.session.add(User(id=user_id, therapy_mode=mode))
        db.session.add(TherapySession(
            id=session_id, mode=mode, created_by=user_id,
            created_at=datetime.now(timezone.utc), therapist_id=None,
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    return session_id, user_id


def test_client_without_consent_is_redirected_to_consent_gate(client):
    """A client must pass the consent gate BEFORE the room loads, so no session
    content is ever on screen before they agree. Hitting the room without consent
    redirects to the dedicated /session/<id>/consent screen."""
    session_id, _ = _make_client_session(client)
    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code in (301, 302)
    assert f"/session/{session_id}/consent" in rv.headers.get("Location", "")


def test_consent_gate_records_consent_and_admits_client(client):
    """The consent gate renders the disclosure + agree button; POSTing it records
    the client's agreement (a stored transcript line) and admits them — the
    follow-up room render returns 200."""
    session_id, _ = _make_client_session(client)
    gate = client.get(f"/session/{session_id}/consent")
    assert gate.status_code == 200
    assert b"I understand and agree" in gate.data
    # No session content (chat composer) on the consent screen.
    assert b"Share with the group" not in gate.data

    post = client.post(f"/session/{session_id}/consent")
    assert post.status_code in (301, 302)
    assert f"/therapy/group/{session_id}" in post.headers.get("Location", "")

    room = client.get(f"/therapy/group/{session_id}")
    assert room.status_code == 200

    with app.app_context():
        msgs = ChatMessage.query.filter_by(session_id=session_id).all()
        assert any("[Consent] Agreed" in m.text for m in msgs)


def test_therapist_is_not_consent_gated(client):
    """The therapist leads the session and is never consent-gated: the room
    renders directly and no client consent modal is present."""
    rv = _session_render(client, as_therapist=True)
    assert rv.status_code == 200
    assert b"joinConsentModal" not in rv.data        # therapist is never prompted


def test_control_strip_shows_mic_camera_transcription(client):
    from unittest.mock import patch
    import config
    with patch.object(config, "RTC_ENABLED", True):
        rv = _session_render(client, as_therapist=False)
    assert rv.status_code == 200
    body = rv.data
    assert b"tmControlStrip" in body                  # the always-visible strip
    assert b"Mic off" in body                         # mic pill
    assert b"Camera off" in body                      # camera pill
    assert b"Transcription off" in body               # transcription pill
    assert b"rtcSttBtn" in body


def test_chat_pane_hidden_by_default_for_clients_in_video_sessions(client):
    """In a video (RTC) session the client's chat pane starts collapsed so the
    video is full-width; the clinician keeps the side-by-side view. The Transcript
    toggle stays available so the client can open the chat."""
    from unittest.mock import patch
    import config
    with patch.object(config, "RTC_ENABLED", True):
        client_rv = _session_render(client, as_therapist=False, consented=True)
        ther_rv = _session_render(client, as_therapist=True)
    assert client_rv.status_code == 200 and ther_rv.status_code == 200
    # Client: chat pane collapsed by default, but the toggle is present to open it.
    assert b"session-work has-video chat-hidden" in client_rv.data
    assert b"transcriptToggleBtn" in client_rv.data
    # Clinician: normal side-by-side, chat shown (the work div is not collapsed).
    assert b"session-work has-video chat-hidden" not in ther_rv.data


def test_client_sees_recording_status_in_strip(client):
    from unittest.mock import patch
    import config
    with patch.object(config, "RECORDING_ENABLED", True):
        rv = _session_render(client, as_therapist=False)
    assert rv.status_code == 200
    assert b"recClientStatus" in rv.data              # client read-only recording status
    assert b"Recording off" in rv.data


def test_recording_consent_states_legal_basis(client):
    from unittest.mock import patch
    import config
    with patch.object(config, "RECORDING_ENABLED", True):
        rv = _session_render(client, as_therapist=False)   # recConsentModal is client-only
    assert rv.status_code == 200
    body = rv.data
    assert b"Legal basis:" in body
    assert b"all-party consent" in body
    assert b"all 50 states and the District of Columbia" in body


def test_recording_status_button_says_audio_video(client):
    from unittest.mock import patch
    import config
    with patch.object(config, "RECORDING_ENABLED", True):
        rv = _session_render(client, as_therapist=True)   # Record control is therapist-only
    assert rv.status_code == 200
    assert b"Recording (audio + video) is OFF" in rv.data


# ---------------------------------------------------------------------------
# Privacy banner — close button must be wired up so clicking X actually dismisses
# ---------------------------------------------------------------------------

def _assert_privacy_section_merged(html: bytes):
    """The clinical-record / privacy statement is a short inline note on the single
    compact Session ID line — no longer a separate dismissable or collapsible
    banner."""
    body = html.decode()
    assert "Confidential clinical record" in body
    assert "never sold or used to train AI" in body


def test_couple_privacy_section_merged(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="couple"))
        db.session.add(TherapySession(
            id=session_id, mode="couple", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["consented_sessions"] = [session_id]   # past the consent gate
    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    _assert_privacy_section_merged(rv.data)


# (Pruned test_group_privacy_section_merged — the privacy section is mode-independent
#  and identical to the couple case already covered above.)


# ---------------------------------------------------------------------------
# Transcript download
# ---------------------------------------------------------------------------

def test_transcript_pdf_forbidden_without_session(client):
    """No flask session → 403 regardless of session_id."""
    fake_session_id = generate_session_id()
    rv = client.get(f"/transcript/{fake_session_id}/pdf")
    assert rv.status_code == 403


def test_transcript_docx_forbidden_without_session(client):
    """No flask session → 403 regardless of session_id."""
    fake_session_id = generate_session_id()
    rv = client.get(f"/transcript/{fake_session_id}/docx")
    assert rv.status_code == 403


def test_transcript_pdf_returns_pdf(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{session_id}/pdf")
    assert rv.status_code == 200
    assert rv.content_type == "application/pdf"
    assert rv.data[:4] == b"%PDF"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_docx_returns_docx(client):
    priv, user_id, session_id = _register(client, "solo")
    client.post(f"/therapy/solo/{session_id}", data={"message": "I feel anxious"})

    rv = client.get(f"/transcript/{session_id}/docx")
    assert rv.status_code == 200
    assert "wordprocessingml" in rv.content_type
    # DOCX files are ZIP archives starting with PK
    assert rv.data[:2] == b"PK"
    assert b"attachment" in rv.headers.get("Content-Disposition", "").encode()


def test_transcript_pdf_empty_session(client):
    from datetime import datetime, timezone
    with app.app_context():
        from models import TherapySession
        user_id = str(uuid.uuid4())
        session_id = generate_session_id()
        db.session.add(User(id=user_id, therapy_mode="solo"))
        db.session.add(TherapySession(
            id=session_id, mode="solo", created_by=user_id,
            created_at=datetime.now(timezone.utc)
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.get(f"/transcript/{session_id}/pdf")
    assert rv.status_code == 200
    assert rv.data[:4] == b"%PDF"


def test_client_cannot_download_clinician_led_transcript(client):
    """Clients must not download a clinician-led session's record — only the
    clinician. A client hitting the transcript routes directly gets 403."""
    therapist_id = "therapist-dl"
    user_id = str(uuid.uuid4())
    session_id = generate_session_id()
    with app.app_context():
        db.session.add(User(id=user_id, therapy_mode="group"))
        db.session.add(TherapySession(
            id=session_id, mode="group", created_by=therapist_id,
            created_at=datetime.now(timezone.utc), therapist_id=therapist_id,
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    assert client.get(f"/transcript/{session_id}/pdf").status_code == 403
    assert client.get(f"/transcript/{session_id}/docx").status_code == 403


def test_clinician_can_still_download_their_session_transcript(client):
    """The clinician who led the session can still download its transcript."""
    therapist_id = str(uuid.uuid4())
    session_id = generate_session_id()
    with app.app_context():
        db.session.add(User(id=therapist_id, therapy_mode="group"))
        db.session.add(TherapySession(
            id=session_id, mode="group", created_by=therapist_id,
            created_at=datetime.now(timezone.utc), therapist_id=therapist_id,
        ))
        db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = therapist_id
    rv = client.get(f"/transcript/{session_id}/pdf")
    assert rv.status_code == 200
    assert rv.data[:4] == b"%PDF"


def test_download_button_hidden_for_clients_shown_for_clinician(client):
    """The Download control is clinician-only: the client session page must not
    render the transcript download link; the clinician page must."""
    client_rv = _session_render(client, as_therapist=False, consented=True)
    assert client_rv.status_code == 200
    assert b'/pdf" download' not in client_rv.data        # no download link for clients

    ther_rv = _session_render(client, as_therapist=True)
    assert ther_rv.status_code == 200
    assert b'/pdf" download' in ther_rv.data               # clinician keeps it


def test_end_session_modal_present_in_base(client):
    """The modal container must be in the base layout (rendered on every therapy page)."""
    from datetime import datetime, timezone
    user_id = str(uuid.uuid4())
    session_id = generate_session_id()
    db.session.add(User(id=user_id, therapy_mode="couple"))
    db.session.add(TherapySession(
        id=session_id, mode="couple", created_by=user_id,
        created_at=datetime.now(timezone.utc),
    ))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["consented_sessions"] = [session_id]   # past the consent gate
    rv = client.get(f"/therapy/couple/{session_id}")
    assert b'id="endSessionModal"' in rv.data
    assert b"End this session for everyone?" in rv.data
    # End Session is a native form POST (cache/socket-proof) to /session/<id>/end.
    assert f'action="/session/{session_id}/end"'.encode() in rv.data


# ---------------------------------------------------------------------------
# Session ID centralisation — integration tests
# ---------------------------------------------------------------------------

def test_register_returns_joined_valid_session_id(client):
    """Joining via registration returns the (valid) session_id of the joined session,
    distinct from the new user_id."""
    for mode in ("solo", "couple", "group"):
        priv, user_id, session_id = _register(client, mode)
        assert is_valid_session_id(session_id), (
            f"{mode} session_id {session_id!r} is not a valid randomized session ID"
        )
        assert session_id != user_id, (
            f"{mode} session_id must be independent of user_id"
    )


def test_all_modes_return_different_session_and_user_ids(client):
    """For every mode, session_id must be distinct from user_id (no longer UUID-derived)."""
    for mode in ("solo", "couple", "group"):
        priv, user_id, session_id = _register(client, mode)
        assert session_id != user_id, (
            f"{mode}: session_id should be a randomized private key, not the user UUID"
        )
        assert is_valid_session_id(session_id), (
            f"{mode}: session_id {session_id!r} is not a valid randomized-private-key session ID"
        )


def test_join_page_contains_dynamic_hint(client):
    """The join page help text must come from the module, not hardcoded HTML."""
    rv = client.get("/session/join")
    assert rv.status_code == 200
    body = rv.data.decode()
    # Must contain the correct length (6) not the stale "4-digit"
    assert "6" in body
    assert "4-digit" not in body


def test_join_page_placeholder_uses_charset_example(client):
    """The join page placeholder must show a realistic session ID example."""
    from session_id import _example_session_id
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert _example_session_id().encode() in rv.data


def test_join_page_does_not_say_1234(client):
    """Regression: old placeholder said '1234' implying a 4-digit numeric code."""
    rv = client.get("/session/join")
    assert rv.status_code == 200
    assert b"1234" not in rv.data


def test_couple_page_shows_session_id_in_banner(client):
    """The couple therapy page session banner must show the randomized-private-key session ID."""
    priv, user_id, session_id = _register(client, "couple")

    with client.session_transaction() as sess:
        sess["consented_sessions"] = [session_id]   # past the consent gate

    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    body = rv.data.decode()

    assert session_id in body, f"session_id {session_id!r} not found in couple page"

    import re
    uuid_pattern = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE
    )
    banner_start = body.find("Session ID:")
    banner_section = body[banner_start:banner_start + 200] if banner_start != -1 else ""
    assert not uuid_pattern.search(banner_section), (
        f"Raw UUID found in couple session banner.\nBanner section: {banner_section!r}"
    )


# (Pruned test_group_page_shows_session_id_in_banner — the Session ID banner is
#  mode-independent; the couple variant above covers the same template path.)


def _make_therapist_led_solo(client):
    """Create a therapist-led 1:1 session (the only kind of solo session that
    exists now) and return its id."""
    sid = generate_session_id()
    db.session.add(TherapySession(
        id=sid, mode="solo", created_by="therapist-x",
        created_at=datetime.now(timezone.utc), therapist_id="therapist-x",
    ))
    db.session.commit()
    return sid


def test_join_post_session_id_is_case_insensitive(client):
    """Session IDs are case-insensitive: submitting a wrong-case version of a
    valid ID must still find the session and redirect successfully.

    Architecture: both the stored ID and the submitted input are uppercased
    before comparison, so 'aB3k7M' and 'AB3K7M' resolve to the same session.
    """
    session_id = _make_therapist_led_solo(client)

    # Submit the ID in all-lowercase to guarantee a case mismatch with stored form
    lowered = session_id.lower()

    rv = client.post("/session/join", data={"session_id": lowered},
                     follow_redirects=False)
    # A redirect (not the 200 "not found" page) proves the lowercased ID matched.
    assert rv.status_code in (301, 302), (
        f"Expected redirect for wrong-case ID '{lowered}' (stored as '{session_id}'), "
        f"got {rv.status_code}. Session ID lookup must be case-insensitive."
    )


def test_solo_rejoin_by_session_id(client):
    """Therapist-led 1:1 sessions must be rejoinable by the exact session ID."""
    session_id = _make_therapist_led_solo(client)

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Solo rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_couple_rejoin_by_session_id(client):
    """Couple sessions must be rejoinable by the exact randomized-private-key session ID."""
    priv, user_id, session_id = _register(client, "couple")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Couple rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_group_rejoin_by_session_id(client):
    """Group sessions must be rejoinable by the exact randomized-private-key session ID."""
    priv, user_id, session_id = _register(client, "group")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Group rejoin by session ID '{session_id}' failed — got {rv.status_code}. "
        f"Response: {rv.data[:300]}"
    )


def test_join_error_page_still_shows_hint(client):
    """Even on error, the join page must render the format hint (not blank)."""
    from session_id import _example_session_id
    rv = client.post("/session/join", data={"session_id": "DOESNOTEXIST"})
    assert rv.status_code == 200
    assert b"not found" in rv.data.lower()
    # Hint must still be present — it's passed through _join_template on all paths
    assert _example_session_id().encode() in rv.data


# ---------------------------------------------------------------------------
# Item 8 — Nicknames/labels cannot be used for server-side joining
# ---------------------------------------------------------------------------

def test_solo_nickname_cannot_be_used_to_join(client):
    """Submitting a label/nickname to the join form must return 'not found'.

    Friendly names are local-only (localStorage). The server never translates
    a label to a session ID — only the randomized-private-key session ID is accepted.
    """
    _register(client, "solo")  # creates a session, but we submit a label instead
    rv = client.post("/session/join", data={"session_id": "My Monday session"},
                     follow_redirects=False)
    assert rv.status_code == 200, "Should render error page, not redirect"
    assert b"not found" in rv.data.lower()


def test_couple_nickname_cannot_be_used_to_join(client):
    """Same label-rejection test for couple sessions."""
    _register(client, "couple")
    rv = client.post("/session/join", data={"session_id": "JohnAndJane"},
                     follow_redirects=False)
    assert rv.status_code == 200, "Should render error page, not redirect"
    assert b"not found" in rv.data.lower()


def test_group_nickname_cannot_be_used_to_join(client):
    """Same label-rejection test for group sessions."""
    _register(client, "group")
    rv = client.post("/session/join", data={"session_id": "ThursdayGroup"},
                     follow_redirects=False)
    assert rv.status_code == 200, "Should render error page, not redirect"
    assert b"not found" in rv.data.lower()


# ---------------------------------------------------------------------------
# Item 12 — Second user can join an existing couple or group session
# ---------------------------------------------------------------------------

def test_second_user_can_join_couple_session(client):
    """A second user (no existing session cookie) who submits a couple session ID
    must be redirected to /auth/couple so they can register and join the session.

    This tests the core couple mechanic: User A creates the session, User B
    joins using the shared session ID.
    """
    # User A creates a couple session
    _priv, _user_id, session_id = _register(client, "couple")

    # User B: clear any session cookie so this is a fresh (unauthenticated) user
    with client.session_transaction() as sess:
        sess.clear()

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    # Server must redirect User B to auth/couple (not error, not solo)
    assert rv.status_code in (301, 302), (
        f"Expected redirect to auth page for second user joining couple session, "
        f"got {rv.status_code}"
    )
    location = rv.headers.get("Location", "")
    assert "/auth/couple" in location, (
        f"Second user should be redirected to /auth/couple, got: {location}"
    )


def test_second_user_can_join_group_session(client):
    """A second user (no existing session cookie) who submits a group session ID
    must be redirected to /auth/group so they can register and join the session.
    """
    # User A creates a group session
    _priv, _user_id, session_id = _register(client, "group")

    # User B: clear any session cookie so this is a fresh (unauthenticated) user
    with client.session_transaction() as sess:
        sess.clear()

    rv = client.post("/session/join", data={"session_id": session_id},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Expected redirect to auth page for second user joining group session, "
        f"got {rv.status_code}"
    )
    location = rv.headers.get("Location", "")
    assert "/auth/group" in location, (
        f"Second user should be redirected to /auth/group, got: {location}"
    )


# ---------------------------------------------------------------------------
# Display name — default names, API endpoint, uniqueness, transcripts
# ---------------------------------------------------------------------------

def test_default_display_name_solo():
    from TogetherMindsAI import _default_display_name
    assert _default_display_name("solo", 1) == "Solo1"

def test_default_display_name_couple():
    from TogetherMindsAI import _default_display_name
    assert _default_display_name("couple", 1) == "Partner1"
    assert _default_display_name("couple", 2) == "Partner2"

def test_default_display_name_group():
    from TogetherMindsAI import _default_display_name
    assert _default_display_name("group", 3) == "GroupMember3"

def test_api_display_name_set(client):
    """POST /api/display-name stores name and returns 200."""
    _priv, user_id, session_id = _register(client, "solo")
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.post("/api/display-name",
                     json={"session_id": session_id, "display_name": "Alice"},
                     content_type="application/json")
    assert rv.status_code == 200
    assert rv.get_json()["display_name"] == "Alice"

def test_api_display_name_uniqueness_case_insensitive(client):
    """Second user cannot take a name already claimed (case-insensitive)."""
    from TogetherMindsAI import _claim_display_name
    _priv, user_id, session_id = _register(client, "solo")
    _priv2, user_id2, _ = _register(client, "solo")

    # User 1 claims "Alice"
    _claim_display_name(session_id, user_id, "Alice")

    # User 2 tries "alice" (different case) — should get 409
    with client.session_transaction() as sess:
        sess["user_id"] = user_id2
    rv = client.post("/api/display-name",
                     json={"session_id": session_id, "display_name": "alice"},
                     content_type="application/json")
    assert rv.status_code == 409

def test_api_display_name_user_can_reconfirm_own_name(client):
    """A user can re-submit their own current name without a 409."""
    from TogetherMindsAI import _claim_display_name
    _priv, user_id, session_id = _register(client, "solo")
    _claim_display_name(session_id, user_id, "Alice")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
    rv = client.post("/api/display-name",
                     json={"session_id": session_id, "display_name": "Alice"},
                     content_type="application/json")
    assert rv.status_code == 200

def test_name_permanently_claimed_after_disconnect():
    """Once a user leaves, their name stays in taken_names and cannot be reclaimed."""
    from TogetherMindsAI import (
        _claim_display_name, _is_name_taken,
        session_display_names, session_taken_names,
    )
    sid = "TEST99"
    uid1 = "user-aaa"
    uid2 = "user-bbb"

    # uid1 claims "Sarah"
    _claim_display_name(sid, uid1, "Sarah")
    # uid1 "disconnects" — remove from active names
    session_display_names.get(sid, {}).pop(uid1, None)

    # uid2 tries to claim "sarah" — must still be blocked
    assert _is_name_taken(sid, uid2, "sarah") is True

    # Cleanup
    session_display_names.pop(sid, None)
    session_taken_names.pop(sid, None)


def test_history_includes_current_display_name_when_already_set(client):
    """on_join history emit includes current_display_name when user already has one set,
    so the client can skip the name-prompt modal on page reload."""
    from datetime import datetime, timezone
    from TogetherMindsAI import _claim_display_name, session_display_names, session_taken_names
    user_id = str(uuid.uuid4())
    session_id = generate_session_id()
    db.session.add(User(id=user_id, therapy_mode="couple"))
    db.session.add(TherapySession(
        id=session_id, mode="couple", created_by=user_id,
        created_at=datetime.now(timezone.utc),
    ))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["consented_sessions"] = [session_id]   # past the consent gate

    # Simulate name already claimed (e.g. from first page load)
    _claim_display_name(session_id, user_id, "Riku")

    # Hitting the page again (simulates page reload after message send)
    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200

    # The server-side name is still set
    assert session_display_names.get(session_id, {}).get(user_id) == "Riku"

    # Cleanup
    session_display_names.pop(session_id, None)
    session_taken_names.pop(session_id, None)


# ---------------------------------------------------------------------------
# WebSocket upgrade disabled under werkzeug
# ---------------------------------------------------------------------------

def test_socketio_upgrades_disabled_in_dev_and_test_mode():
    """Regression: werkzeug crashes with AssertionError when socket.io clients
    attempt a WebSocket upgrade (transport=websocket POST returns 500).
    The SocketIO instance must disable upgrades whenever FLASK_DEBUG or
    IS_TESTING is True so clients never make the upgrade attempt."""
    import config
    from TogetherMindsAI import socketio
    is_werkzeug_env = config.FLASK_DEBUG or config.IS_TESTING
    if is_werkzeug_env:
        assert socketio.server.eio.allow_upgrades is False, (
            "allow_upgrades must be False under werkzeug (FLASK_DEBUG or IS_TESTING) "
            "to prevent the AssertionError: write() before start_response crash"
        )
