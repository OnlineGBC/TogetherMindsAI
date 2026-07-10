"""
tests/test_progress_embed.py
----------------------------
The live-session Progress button opens the Progress page inside a modal iframe
(?embed=1) instead of navigating away. Navigating away used to trip the
beforeunload "Leave site?" popup; the embedded render avoids it entirely.

These tests lock in that the embedded view drops the site chrome (so it sits
cleanly in the modal), still shows the real content, and keeps the owner-only
access check.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from TogetherMindsAI import app
from models import db


@pytest.fixture
def client():
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


def _login(client, uid):
    with client.session_transaction() as s:
        s["user_id"] = uid


def test_progress_full_page_has_site_chrome(client):
    """The standalone Progress page keeps the navbar, crisis bar, and Back button."""
    uid = "u-full"
    _login(client, uid)
    body = client.get(f"/progress/{uid}/solo").get_data(as_text=True)
    assert "navbar-brand" in body          # the real top nav element
    assert "disclaimer-bar" in body        # the crisis bar
    assert "Back to Solo Therapy" in body
    assert "Your Progress" in body


def test_progress_embed_strips_chrome_for_modal(client):
    """?embed=1 renders without the navbar/crisis bar/footer or the redundant
    Back button, but still shows the real progress content — so it fits the
    live-session modal iframe without looking like a page-in-a-page."""
    uid = "u-embed"
    _login(client, uid)
    resp = client.get(f"/progress/{uid}/solo?embed=1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "navbar-brand" not in body      # no top nav in the embedded view
    assert "disclaimer-bar" not in body    # no crisis bar
    assert "Back to Solo Therapy" not in body
    assert "Your Progress" in body


def test_progress_embed_still_enforces_owner_only(client):
    """The IDOR guard must still apply in embed mode — you can only view your own."""
    _login(client, "owner")
    resp = client.get("/progress/someone-else/solo?embed=1")
    assert resp.status_code == 403
