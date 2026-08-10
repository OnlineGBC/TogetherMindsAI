"""
tests/test_error_pages.py
-------------------------
Branded 404 / 500 pages instead of Werkzeug's raw ones.

The important property: a mistyped URL and a page the caller isn't allowed to see
must produce the SAME response, so the 404 never reveals which it was.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-errors")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

import TogetherMindsAI as tm
from TogetherMindsAI import app
from models import db, init_encryption

init_encryption(TEST_KEY)


@pytest.fixture
def client():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._app_engines[app] = {None: engine}
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        with app.test_client() as c:
            yield c
        db.session.remove()
        db.drop_all()


def test_unknown_url_gets_the_branded_page(client):
    rv = client.get("/ggg")
    assert rv.status_code == 404
    assert b"We couldn't find that page" in rv.data
    # Werkzeug's raw wording is gone.
    assert b"The requested URL was not found on the server" not in rv.data


def test_signed_out_404_offers_a_way_to_log_in(client):
    rv = client.get("/ggg")
    assert b"Log in" in rv.data
    assert b"Home" in rv.data


def test_hidden_page_is_indistinguishable_from_a_typo(client):
    """/accessadmin (not an admin) and /ggg (does not exist) must look identical,
    so the response never confirms the console exists."""
    typo = client.get("/ggg")
    hidden = client.get("/accessadmin")
    assert typo.status_code == hidden.status_code == 404
    assert typo.data == hidden.data


def test_api_paths_get_json_not_html(client):
    rv = client.get("/api/does-not-exist")
    assert rv.status_code == 404
    assert rv.is_json
    assert rv.get_json()["error"] == "not_found"


def test_crash_renders_the_branded_500(client):
    """A genuine unhandled exception returns the branded page, not a stack trace.

    Routes can't be added after the first request, so an existing view is swapped
    for one that raises, then restored.
    """
    original = app.view_functions["root"]

    def _boom(*args, **kwargs):
        raise RuntimeError("deliberate test crash")

    app.view_functions["root"] = _boom
    app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        rv = client.get("/")
        assert rv.status_code == 500
        assert b"Something went wrong" in rv.data
        assert b"deliberate test crash" not in rv.data      # no internals leaked
    finally:
        app.view_functions["root"] = original
        app.config.pop("PROPAGATE_EXCEPTIONS", None)


def test_http_errors_keep_their_own_status(client):
    """The catch-all Exception handler must not turn a 404 into a 500."""
    app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        assert client.get("/still-not-here").status_code == 404
    finally:
        app.config.pop("PROPAGATE_EXCEPTIONS", None)
