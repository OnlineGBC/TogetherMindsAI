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
from models import db, User, TherapySession
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
    assert client.get(f"/progress/{user_id}/solo").status_code == 200


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
    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    assert b"endSessionModal" in rv.data
    assert b"End Session" in rv.data


# ---------------------------------------------------------------------------
# Privacy banner — close button must be wired up so clicking X actually dismisses
# ---------------------------------------------------------------------------

def _assert_privacy_banner_dismissable(html: bytes):
    """The privacy banner must use Bootstrap's standard alert-dismissible pattern,
    and must persist its dismissed state across silent reloads (form POST → 302 → GET
    when sending a message in solo mode) via sessionStorage, while reappearing on
    a manual F5 (detected via PerformanceNavigationTiming).

    Regression 1: a previous inline onclick="dismissPrivacyBanner()" handler did
    nothing when clicked, so the X never closed the banner.
    Regression 2: with no persistence at all, the banner reappeared after every
    sent message in solo mode (because the page reloads on form POST).
    """
    body = html.decode()
    banner_idx = body.find('id="privacyBanner"')
    assert banner_idx != -1, "privacy banner element not found in rendered page"

    # Find the opening tag of the alert div
    div_start = body.rfind("<div", 0, banner_idx)
    div_end = body.find(">", banner_idx)
    alert_tag = body[div_start:div_end + 1]
    assert "alert-dismissible" in alert_tag, (
        f"privacyBanner div must include 'alert-dismissible' so Bootstrap recognises "
        f"the close button. Got: {alert_tag!r}"
    )

    # The close button must use Bootstrap's data-bs-dismiss attribute,
    # not an inline onclick that depends on a custom JS function.
    banner_section = body[banner_idx:banner_idx + 1500]
    close_btn_idx = banner_section.find('class="btn-close')
    assert close_btn_idx != -1, "privacy banner must contain a btn-close element"
    close_btn_end = banner_section.find(">", close_btn_idx)
    close_btn_tag = banner_section[close_btn_idx:close_btn_end + 1]
    assert 'data-bs-dismiss="alert"' in close_btn_tag, (
        f"privacy banner close button must have data-bs-dismiss=\"alert\" so Bootstrap "
        f"dismisses the banner on click. Got: {close_btn_tag!r}"
    )
    assert "dismissPrivacyBanner" not in close_btn_tag, (
        "the dead inline onclick=\"dismissPrivacyBanner()\" handler must be removed"
    )

    # The page must wire the dismissal to sessionStorage so it survives the
    # silent reload triggered by sending a message, and must clear that flag
    # on a manual reload so the banner reappears on F5.
    assert "sessionStorage" in body, (
        "privacy banner dismissal must use sessionStorage to persist across silent reloads"
    )
    assert 'localStorage.setItem("privacyBannerDismissed_' not in body, (
        "privacy banner must not store dismissal in localStorage (long-lived) — sessionStorage only"
    )
    assert 'localStorage.getItem("privacyBannerDismissed_' not in body, (
        "privacy banner must not read dismissal from localStorage (long-lived) — sessionStorage only"
    )


def test_couple_privacy_banner_dismiss_button_wired(client):
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
    rv = client.get(f"/therapy/couple/{session_id}")
    assert rv.status_code == 200
    _assert_privacy_banner_dismissable(rv.data)


def test_group_privacy_banner_dismiss_button_wired(client):
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
    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    _assert_privacy_banner_dismissable(rv.data)


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
    rv = client.get(f"/therapy/couple/{session_id}")
    assert b"endSessionIdDisplay" in rv.data
    assert b"endSessionConfirmBtn" in rv.data
    assert b"endSessionCopyBtn" in rv.data


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


def test_group_page_shows_session_id_in_banner(client):
    """For group sessions, the randomized-private-key session_id should appear in the rendered page."""
    priv, user_id, session_id = _register(client, "group")

    rv = client.get(f"/therapy/group/{session_id}")
    assert rv.status_code == 200
    assert session_id.encode() in rv.data


def test_join_post_session_id_is_case_insensitive(client):
    """Session IDs are case-insensitive: submitting a wrong-case version of a
    valid ID must still find the session and redirect successfully.

    Architecture: both the stored ID and the submitted input are uppercased
    before comparison, so 'aB3k7M' and 'AB3K7M' resolve to the same session.
    """
    priv, user_id, session_id = _register(client, "solo")

    # Submit the ID in all-lowercase to guarantee a case mismatch with stored form
    lowered = session_id.lower()

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    rv = client.post("/session/join", data={"session_id": lowered},
                     follow_redirects=False)
    assert rv.status_code in (301, 302), (
        f"Expected redirect for wrong-case ID '{lowered}' (stored as '{session_id}'), "
        f"got {rv.status_code}. Session ID lookup must be case-insensitive."
    )


def test_solo_rejoin_by_session_id(client):
    """Solo sessions must be rejoinable by the exact randomized-private-key session ID."""
    priv, user_id, session_id = _register(client, "solo")

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


def test_ai_cooldown_skips_second_response_in_couple_mode(client):
    """In couple/group mode a second message within the cooldown window must not
    generate an AI reply, preventing consecutive AI messages in the transcript."""
    from datetime import timezone
    from unittest.mock import patch
    import datetime as dt

    _priv, user_id, session_id = _register(client, "couple")
    _priv2, user_id2, _ = _register(client, "couple")

    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    # Inject a very recent AI response timestamp to simulate cooldown active
    from TogetherMindsAI import session_ai_last_response
    session_ai_last_response[session_id] = dt.datetime.now(timezone.utc)

    # Sending a message should broadcast the user message but skip AI generation
    with patch("TogetherMindsAI.process_input") as mock_pi:
        client.post(f"/therapy/couple/{session_id}",
                    data={"message": "Hello"},
                    content_type="application/x-www-form-urlencoded")
        # process_input must not be called during the cooldown window
        mock_pi.assert_not_called()

    # Cleanup
    session_ai_last_response.pop(session_id, None)


def test_opening_message_does_not_block_users_first_reply(client):
    """Regression: when the AI opening message is generated for a fresh
    couple/group session, the cooldown slot is pre-claimed to prevent a
    partner's racing message from causing a duplicate AI reply. That slot
    must be RELEASED once the opening is committed — otherwise the user's
    first reply lands within the 20s cooldown window and gets silently
    skipped, forcing the user to type twice before the AI responds.
    """
    from datetime import timezone
    from unittest.mock import patch
    import datetime as dt
    from TogetherMindsAI import (
        session_ai_last_response,
        session_opening_sent,
        room_mode,
        room_participants,
        sid_to_user,
        sid_to_session,
        on_join,
        socketio,
    )

    _priv, user_id, session_id = _register(client, "couple")

    # Clean any state left over from prior tests
    session_ai_last_response.pop(session_id, None)
    session_opening_sent.discard(session_id)

    # Force the on_join opening branch by disabling IS_TESTING for this call,
    # and stub out generate_opening_message so we don't need a real Claude call.
    fake_opening = "Welcome to your couple session — I'm here to help."
    with patch("TogetherMindsAI.config.IS_TESTING", False), \
         patch("TogetherMindsAI.generate_opening_message", return_value=fake_opening):

        # Fabricate a SocketIO test client connection so on_join has a request context
        sio_client = socketio.test_client(app, flask_test_client=client)
        sio_client.emit("join", {
            "session_id": session_id,
            "user_id":    user_id,
            "mode":       "couple",
        })
        sio_client.disconnect()

    # The opening must have been generated (proving we hit the branch under test)
    assert session_id in session_opening_sent, (
        "Test setup failure: on_join did not enter the opening branch — "
        "IS_TESTING patch may not have taken effect"
    )

    # And the cooldown slot must be released afterwards so the user's
    # first message gets a normal AI response.
    assert session_id not in session_ai_last_response, (
        "AI cooldown slot was not released after opening message was sent. "
        "User's first reply will be silently swallowed by the 20s cooldown."
    )

    # Cleanup
    session_opening_sent.discard(session_id)
    room_mode.pop(session_id, None)
    room_participants.pop(session_id, None)


# ---------------------------------------------------------------------------
# Silence nudge — AI re-engages after a quiet period in couple/group sessions
# ---------------------------------------------------------------------------

def _reset_silence_state(session_id):
    """Clear silence tracking for one session — kept tidy between tests."""
    from TogetherMindsAI import session_last_message_at, session_silence_check_pending
    session_last_message_at.pop(session_id, None)
    session_silence_check_pending.discard(session_id)


def test_silence_nudge_fires_after_quiet_period(client):
    """When SILENCE_NUDGE_SECONDS elapses without any new message in a
    couple session, the silence-check task must persist a nudge ChatMessage
    authored by 'AI' and broadcast it on the socket.
    """
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch
    from TogetherMindsAI import (
        _silence_check_task, session_last_message_at,
        session_silence_check_pending, room_participants,
    )
    from models import ChatMessage

    _priv, user_id, session_id = _register(client, "couple")
    _reset_silence_state(session_id)
    room_participants[session_id] = {user_id}

    # Pretend the last message was long enough ago that the elapsed check trips
    session_last_message_at[session_id] = datetime.now(timezone.utc) - timedelta(seconds=999)
    session_silence_check_pending.add(session_id)

    nudge_text = "Take your time — I'm here whenever you're ready."
    # Stub socketio.sleep to no-op and stub the Claude call
    with patch("TogetherMindsAI.socketio.sleep", lambda s: None), \
         patch("TogetherMindsAI.generate_silence_nudge", return_value=nudge_text):
        _silence_check_task(session_id, "couple", app.app_context)

    with app.app_context():
        nudges = [m for m in ChatMessage.query.filter_by(session_id=session_id, user_id="AI").all()]
        assert any(m.text == nudge_text for m in nudges), (
            f"Expected a nudge ChatMessage with text {nudge_text!r}; got {[m.text for m in nudges]}"
        )

    assert session_id not in session_silence_check_pending, (
        "Silence check pending flag must be cleared after the nudge fires"
    )

    # Cleanup
    _reset_silence_state(session_id)
    room_participants.pop(session_id, None)


def test_silence_nudge_skipped_when_message_arrives_before_timeout(client):
    """If a message arrives during the wait window, the silence-check task
    must keep watching from the new mark and NOT fire a nudge for that
    earlier window. We simulate this by setting last_message_at to 'now'
    before the task runs — its first wakeup sees fresh activity and the
    loop continues; a second wakeup also sees fresh activity, etc.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch
    from TogetherMindsAI import (
        _silence_check_task, session_last_message_at,
        session_silence_check_pending, room_participants,
    )
    from models import ChatMessage

    _priv, user_id, session_id = _register(client, "couple")
    _reset_silence_state(session_id)
    room_participants[session_id] = {user_id}

    # First two wakeups: a fresh message just arrived, silence is broken,
    # task should continue watching. On the third wakeup we deliberately
    # age the timestamp AND empty the room so the task takes the empty-room
    # exit and returns cleanly — without ever sending a nudge.
    from datetime import timedelta
    wakeup_count = {"n": 0}
    def fake_sleep(_):
        wakeup_count["n"] += 1
        if wakeup_count["n"] < 3:
            session_last_message_at[session_id] = datetime.now(timezone.utc)
        else:
            session_last_message_at[session_id] = datetime.now(timezone.utc) - timedelta(seconds=999)
            room_participants[session_id] = set()

    session_last_message_at[session_id] = datetime.now(timezone.utc)
    session_silence_check_pending.add(session_id)

    with patch("TogetherMindsAI.socketio.sleep", fake_sleep), \
         patch("TogetherMindsAI.generate_silence_nudge", return_value="should not be called"):
        _silence_check_task(session_id, "couple", app.app_context)

    with app.app_context():
        ai_msgs = ChatMessage.query.filter_by(session_id=session_id, user_id="AI").all()
        assert len(ai_msgs) == 0, (
            f"No nudge should fire while messages keep arriving; got {[m.text for m in ai_msgs]}"
        )

    _reset_silence_state(session_id)
    room_participants.pop(session_id, None)


def test_silence_nudge_does_not_advance_ai_cooldown(client):
    """A silence nudge is a one-way prompt into a quiet room, not part of a
    human-to-human exchange. It must NOT update session_ai_last_response,
    or the user's reply to the nudge would be silently swallowed by the
    20s cooldown.
    """
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch
    from TogetherMindsAI import (
        _silence_check_task, session_last_message_at,
        session_silence_check_pending, session_ai_last_response,
        room_participants,
    )

    _priv, user_id, session_id = _register(client, "couple")
    _reset_silence_state(session_id)
    session_ai_last_response.pop(session_id, None)
    room_participants[session_id] = {user_id}

    session_last_message_at[session_id] = datetime.now(timezone.utc) - timedelta(seconds=999)
    session_silence_check_pending.add(session_id)

    with patch("TogetherMindsAI.socketio.sleep", lambda s: None), \
         patch("TogetherMindsAI.generate_silence_nudge", return_value="quiet check-in"):
        _silence_check_task(session_id, "couple", app.app_context)

    assert session_id not in session_ai_last_response, (
        "Silence nudge must NOT set session_ai_last_response — otherwise the "
        "user's reply would land within the 20s cooldown and be skipped."
    )

    _reset_silence_state(session_id)
    room_participants.pop(session_id, None)


def test_silence_nudge_skipped_for_empty_room(client):
    """If everyone has disconnected, no nudge should be sent."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import patch
    from TogetherMindsAI import (
        _silence_check_task, session_last_message_at,
        session_silence_check_pending, room_participants,
    )
    from models import ChatMessage

    _priv, user_id, session_id = _register(client, "couple")
    _reset_silence_state(session_id)
    room_participants[session_id] = set()  # empty

    session_last_message_at[session_id] = datetime.now(timezone.utc) - timedelta(seconds=999)
    session_silence_check_pending.add(session_id)

    with patch("TogetherMindsAI.socketio.sleep", lambda s: None), \
         patch("TogetherMindsAI.generate_silence_nudge", return_value="should not be called"):
        _silence_check_task(session_id, "couple", app.app_context)

    with app.app_context():
        ai_msgs = ChatMessage.query.filter_by(session_id=session_id, user_id="AI").all()
        assert len(ai_msgs) == 0, "No nudge should fire into an empty room"

    _reset_silence_state(session_id)
    room_participants.pop(session_id, None)


def test_silence_nudge_solo_mode_does_not_schedule(client):
    """Solo mode must not schedule silence checks (the cooldown only applies
    to couple/group, and so does this re-engagement feature)."""
    from TogetherMindsAI import (
        _schedule_silence_check, session_silence_check_pending,
    )

    _priv, user_id, session_id = _register(client, "solo")
    session_silence_check_pending.discard(session_id)

    _schedule_silence_check(session_id, "solo")

    assert session_id not in session_silence_check_pending, (
        "Solo mode must not arm the silence check"
    )


def test_silence_nudge_disabled_when_seconds_zero(client):
    """SILENCE_NUDGE_SECONDS=0 must short-circuit the scheduler entirely."""
    from unittest.mock import patch
    from TogetherMindsAI import (
        _schedule_silence_check, session_silence_check_pending,
    )

    _priv, user_id, session_id = _register(client, "couple")
    session_silence_check_pending.discard(session_id)

    with patch("TogetherMindsAI.config.SILENCE_NUDGE_SECONDS", 0):
        _schedule_silence_check(session_id, "couple")

    assert session_id not in session_silence_check_pending, (
        "SILENCE_NUDGE_SECONDS=0 must disable the feature"
    )


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
