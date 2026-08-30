"""
tests/test_admin_audit_view.py
------------------------------
Reading the audit log from /accessadmin, and coming back to the page you asked
for after signing in.

Everything was already recorded; there was simply no way to look at it without
database access. The view is read-only by construction — the table is
append-only and hash-chained, and no route offers a delete.
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
os.environ.setdefault("SECRET_KEY", "test-secret-audit-view")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone, timedelta

import admin_access
import config
import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, AuditLog

init_encryption(TEST_KEY)

ADMIN = "raja@onlinegbc.com"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


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


def _clinician(uid, email):
    db.session.add(Clinician(id=uid, provider="google", provider_subject=uid,
                             email=email, role=roles.PSYCHOTHERAPIST,
                             created_at=datetime.now(timezone.utc)))
    db.session.commit()


def _entry(event, user_id=None, details="{}", days_ago=0):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.add(AuditLog(
        event_type=event, user_id=user_id, details=details,
        prev_hash="0" * 64, row_hash=os.urandom(16).hex() + os.urandom(16).hex(),
        timestamp=when, timestamp_str=when.isoformat()))
    db.session.commit()


def _as_verified_admin(client, fn):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            _clinician("admin", ADMIN)
        with client.session_transaction() as s:
            s["user_id"] = "admin"
        import pyotp
        client.post("/accessadmin/verify",
                    data={"totp": pyotp.TOTP(TOTP_SECRET).now()},
                    follow_redirects=True)
        return fn()
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# The search itself
# ---------------------------------------------------------------------------

def test_admin_actions_are_shown_and_session_noise_is_not(client):
    """The log covers the whole app — thousands of session rows a day. Those are
    noise on this page, so they sit behind a toggle rather than the default."""
    with app.app_context():
        _entry("role_changed")
        _entry("session_created")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician)
        assert [r.event_type for r in rows] == ["role_changed"]


def test_the_toggle_shows_everything(client):
    with app.app_context():
        _entry("role_changed")
        _entry("session_created")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, admin_only=False)
        assert len(rows) == 2


def test_filter_by_event_type(client):
    with app.app_context():
        _entry("role_changed")
        _entry("account_disabled")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, event="account_disabled")
        assert [r.event_type for r in rows] == ["account_disabled"]


def test_filter_by_who_matches_an_email(client):
    """The log stores an id, and emails are encrypted with a non-deterministic
    cipher — so a SQL LIKE could never match one. The lookup decrypts the (small)
    account list in Python instead."""
    with app.app_context():
        _clinician("doc-1", "meera@onlinegbc.com")
        _clinician("doc-2", "someone@example.com")
        _entry("role_changed", user_id="doc-1")
        _entry("role_changed", user_id="doc-2")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, who="meera")
        assert [r.user_id for r in rows] == ["doc-1"]


def test_filter_by_who_also_finds_the_person_acted_upon(client):
    """A role change records the ADMIN as user_id and the target inside details.
    Someone typing a name means either reading of "who"."""
    with app.app_context():
        _clinician("target", "target@example.com")
        _entry("role_changed", user_id="admin-1",
               details='{"target_id": "target", "new_role": "caregiver"}')
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, who="target@")
        assert len(rows) == 1


def test_who_matching_nobody_returns_nothing(client):
    with app.app_context():
        _entry("role_changed", user_id="doc-1")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, who="nobody-here")
        assert rows == []


def test_filter_by_date_range(client):
    with app.app_context():
        _entry("role_changed", days_ago=10)
        _entry("role_changed", days_ago=0)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, date_from=today)
        assert len(rows) == 1


def test_a_junk_date_is_ignored_rather_than_crashing(client):
    with app.app_context():
        _entry("role_changed")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, date_from="not-a-date")
        assert len(rows) == 1


def test_filter_by_free_text_in_the_details(client):
    with app.app_context():
        _entry("role_changed", details='{"new_role": "caregiver"}')
        _entry("role_changed", details='{"new_role": "hypnotherapist"}')
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, text="caregiver")
        assert len(rows) == 1


def test_filters_combine(client):
    with app.app_context():
        _clinician("doc-1", "meera@onlinegbc.com")
        _entry("role_changed", user_id="doc-1", details='{"new_role": "caregiver"}')
        _entry("account_disabled", user_id="doc-1")
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician,
                                            who="meera", event="role_changed")
        assert len(rows) == 1


def test_it_says_when_it_is_not_showing_everything(client):
    """Silently truncating would read as "this is all of them"."""
    with app.app_context():
        for _ in range(4):
            _entry("role_changed")
        rows, truncated = admin_access.search_audit(db, AuditLog, Clinician, limit=2)
        assert len(rows) == 2 and truncated is True


def test_newest_first(client):
    with app.app_context():
        _entry("role_changed", details="older", days_ago=5)
        _entry("account_disabled", details="newer", days_ago=0)
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician)
        assert rows[0].details == "newer"


def test_an_undecryptable_account_does_not_break_the_search(client):
    """One bad row must not take the whole page down."""
    with app.app_context():
        _clinician("doc-1", "fine@example.com")
        db.session.execute(
            db.text("UPDATE clinicians SET email = 'not-encrypted' WHERE id = 'doc-1'"))
        db.session.commit()
        rows, _ = admin_access.search_audit(db, AuditLog, Clinician, who="anything")
        assert rows == []


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def test_the_console_shows_the_activity_card(client):
    def check():
        with app.app_context():
            _entry("role_changed", user_id="admin",
                   details='{"new_role": "caregiver"}')
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "Recent activity" in html
        assert "role_changed" in html
    _as_verified_admin(client, check)


def test_ids_are_shown_as_the_account_they_belong_to(client):
    """A bare UUID means nothing on screen."""
    def check():
        with app.app_context():
            _clinician("doc-1", "meera@onlinegbc.com")
            _entry("role_changed", user_id="doc-1")
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "meera@onlinegbc.com" in html
    _as_verified_admin(client, check)


def test_the_filters_come_from_the_query_string(client):
    """So a useful view can be bookmarked or handed to the next admin."""
    def check():
        with app.app_context():
            _entry("role_changed")
            _entry("account_disabled")
        html = client.get("/accessadmin?event=account_disabled").get_data(as_text=True)
        # The table body, not the filter dropdown, is what must be filtered.
        assert '<td class="small">account_disabled</td>' in html
        assert '<td class="small">role_changed</td>' not in html
    _as_verified_admin(client, check)


def test_an_unverified_admin_cannot_read_the_log(client):
    patches = [patch.object(config, "ADMIN_EMAILS", [ADMIN]),
               patch.object(config, "ADMIN_CONSOLE_ENABLED", True),
               patch.object(config, "ADMIN_TOTP_SECRET", TOTP_SECRET)]
    for p in patches:
        p.start()
    try:
        with app.app_context():
            _clinician("admin", ADMIN)
            _entry("role_changed", details="secret-detail")
        with client.session_transaction() as s:
            s["user_id"] = "admin"          # signed in, second factor NOT passed
        html = client.get("/accessadmin").get_data(as_text=True)
        assert "Recent activity" not in html
        assert "secret-detail" not in html
    finally:
        for p in patches:
            p.stop()


def test_a_non_admin_cannot_read_the_log(client):
    with patch.object(config, "ADMIN_EMAILS", [ADMIN]), \
         patch.object(config, "ADMIN_CONSOLE_ENABLED", True):
        with app.app_context():
            _clinician("someone", "someone@example.com")
            _entry("role_changed", details="secret-detail")
        with client.session_transaction() as s:
            s["user_id"] = "someone"
        rv = client.get("/accessadmin")
        assert rv.status_code == 404
        assert b"secret-detail" not in rv.data


def test_the_view_offers_no_way_to_change_the_log():
    """The table is append-only and hash-chained. Nothing that reads it may write.

    Checks the audit functions BY NAME rather than "everything below a comment" —
    the first version did the latter and broke the moment an unrelated section was
    appended to the file, which is a test reporting on the wrong code.
    """
    import inspect
    import admin_access as aa
    for fn in (aa.search_audit, aa._accounts_matching, aa._account_ids,
               aa._email_of, aa._parse_day, aa.audit_event_types,
               aa.account_labels, aa.db_distinct):
        body = inspect.getsource(fn)
        for forbidden in ("db.session.delete", "AuditLog(", ".update(",
                          "db.session.commit", "db.session.add"):
            assert forbidden not in body, f"{fn.__name__}: {forbidden}"


# ---------------------------------------------------------------------------
# Coming back to the page you asked for
# ---------------------------------------------------------------------------

def test_a_refused_page_is_remembered_for_after_sign_in(client):
    """Asking for /accessadmin while signed out used to sign you in and drop you
    on the dashboard."""
    with patch.object(config, "ADMIN_EMAILS", [ADMIN]), \
         patch.object(config, "ADMIN_CONSOLE_ENABLED", True):
        assert client.get("/accessadmin").status_code == 404
    with client.session_transaction() as s:
        assert s.get("post_login_next") == "/accessadmin"


def test_any_path_is_remembered_including_one_that_does_not_exist(client):
    """Costs nothing — that one simply 404s again — and keeps the two pages
    indistinguishable, which is the point."""
    assert client.get("/hack_slug").status_code == 404
    with client.session_transaction() as s:
        assert s.get("post_login_next") == "/hack_slug"


def test_the_remembered_path_is_never_put_in_the_page(client):
    """A visible ?next= would appear only for paths that really exist, telling a
    prober that /accessadmin is a real page — exactly what returning 404 instead
    of 403 is meant to hide."""
    with patch.object(config, "ADMIN_EMAILS", [ADMIN]), \
         patch.object(config, "ADMIN_CONSOLE_ENABLED", True):
        refused = client.get("/accessadmin").get_data(as_text=True)
    missing = client.get("/hack_slug").get_data(as_text=True)
    for html in (refused, missing):
        assert "We couldn't find that page" in html
        assert "next=" not in html
    assert "accessadmin" not in refused


def test_an_off_site_path_is_not_remembered(client):
    """_safe_next guards the open redirect; this pins that the 404 handler uses
    it rather than stashing whatever arrived."""
    client.get("//evil.example.com/x")
    with client.session_transaction() as s:
        assert s.get("post_login_next") is None
