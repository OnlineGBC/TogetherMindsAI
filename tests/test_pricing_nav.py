"""
tests/test_pricing_nav.py
-------------------------
The Pricing link in the header.

It used to sit inside the clinician-only branch of the navbar, so someone who had
not signed in could not find out what the software costs — which is backwards for
a price. There is exactly ONE header (templates/base.html); every page extends it,
and base_embed.html deliberately has none because it renders inside an iframe. So
these tests check the one nav and the pages that reach it.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import re
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-pricing")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

TEST_KEY = Fernet.generate_key().decode()
os.environ["FIELD_ENCRYPTION_KEY"] = TEST_KEY

from datetime import datetime, timezone

import roles
from TogetherMindsAI import app
from models import db, init_encryption, Clinician, ClientAccount

init_encryption(TEST_KEY)

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


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


def _nav(html):
    """Just the navbar, so a Pricing mention elsewhere on the page cannot make a
    test pass while the header is still missing it."""
    m = re.search(r"<nav.*?</nav>", html, re.S)
    return m.group(0) if m else ""


def _clinician(cid="clin-1"):
    db.session.add(Clinician(id=cid, provider="google", provider_subject=cid,
                             email="c@example.com", role=roles.PSYCHOTHERAPIST,
                             created_at=datetime.now(timezone.utc)))
    db.session.commit()


def _client_account(cid="cli-1"):
    db.session.add(ClientAccount(id=cid, provider="google", provider_subject=cid,
                                 created_at=datetime.now(timezone.utc)))
    db.session.commit()


# ---------------------------------------------------------------------------
# Who can see it
# ---------------------------------------------------------------------------

def test_a_visitor_who_has_not_signed_in_can_see_the_price(client):
    """The whole point. A price nobody can find is not a price."""
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert "Pricing" in nav
    assert "/pricing" in nav


def test_a_signed_in_client_can_see_the_price(client):
    """Missed in the original report, and missing for the same reason: the link
    lived in the clinician branch."""
    with app.app_context():
        _client_account()
    with client.session_transaction() as s:
        s["client_account_id"] = "cli-1"
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert "Pricing" in nav


def test_a_clinician_still_sees_it(client):
    with app.app_context():
        _clinician()
    with client.session_transaction() as s:
        s["clinician_id"] = "clin-1"
        s["user_id"] = "clin-1"
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert "Pricing" in nav


def test_it_is_the_last_link_for_a_visitor(client):
    """Asked for as the rightmost entry."""
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    hrefs = re.findall(r'class="nav-link" href="([^"]+)"', nav)
    assert hrefs[-1].endswith("/pricing"), hrefs


def test_a_clinician_keeps_the_position_the_old_link_had(client):
    """Third: Home, My sessions, Pricing. Asked to leave it where it was."""
    with app.app_context():
        _clinician()
    with client.session_transaction() as s:
        s["clinician_id"] = "clin-1"
        s["user_id"] = "clin-1"
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    hrefs = re.findall(r'class="nav-link" href="([^"]+)"', nav)
    assert len(hrefs) >= 3
    assert hrefs[2].endswith("/pricing"), hrefs


def test_the_sign_in_links_are_still_there_for_a_visitor(client):
    """Adding Pricing must not have displaced anything."""
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert "Clinician sign-in" in nav
    assert "Client sign-in" in nav
    assert "Home" in nav


def test_every_page_that_uses_the_header_shows_it(client):
    """One header, so this is really a check that these pages use it — and that
    none of them renders a nav of its own."""
    asserted = []
    for path in ("/welcome", "/client/login", "/login", "/pricing", "/privacy",
                 "/tos"):
        rv = client.get(path, follow_redirects=True)
        if rv.status_code != 200:
            continue                       # not reachable anonymously; not a nav bug
        html = rv.get_data(as_text=True)
        if "<nav" not in html:
            continue                       # no header on this page at all
        assert "Pricing" in _nav(html), path
        asserted.append(path)
    # A floor, so the skips above can never quietly turn this into a test of
    # nothing. All six are reachable without signing in today.
    assert len(asserted) == 6, asserted


# ---------------------------------------------------------------------------
# The one screen it must NOT appear on
# ---------------------------------------------------------------------------

def _render_header(**ctx):
    """The header as a browser would receive it, for a given context.

    Rendered rather than read as text: an earlier version of this test compared
    the positions of `in_live_session` and `pricing_page` in the source, which
    passed even with the link moved OUT of the block — there is a later
    {% endif %} (the account menu's) that satisfied the position check.
    """
    from flask import render_template
    # render_template, not jinja_env.render: the header reads values supplied by
    # the app's context processors (the disclaimer wording, the CSRF token), and
    # rendering through Jinja directly skips those entirely.
    with app.test_request_context("/"):
        html = render_template("base.html", **ctx)
    return _nav(html)


def test_the_live_session_header_stays_focused(client):
    """Mid-call the nav hides Home and the sessions list so nobody wanders out of
    a session. Offering to browse prices during someone's therapy is exactly that."""
    assert "Pricing" not in _render_header(in_live_session=True)


def test_it_is_the_live_session_flag_that_hides_it(client):
    """The other half: without the flag the same header DOES show Pricing, so the
    test above is about the flag and not about some other accident."""
    assert "Pricing" in _render_header(in_live_session=False)


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------

def test_the_price_is_at_pricing(client):
    assert client.get("/pricing").status_code == 200


def test_the_old_address_still_works(client):
    """Recording-reminder emails already sent to real clinicians carry a /billing
    link. Those must not land on a 404 months later."""
    rv = client.get("/billing")
    assert rv.status_code == 301
    assert rv.headers["Location"].endswith("/pricing")


def test_the_old_address_keeps_the_query_string(client):
    """Any Stripe Checkout created before the page moved has a success_url of
    /billing?success=1. Dropping the query string would send those customers to a
    page that never tells them their payment worked."""
    rv = client.get("/billing?success=1")
    assert rv.status_code == 301
    assert rv.headers["Location"].endswith("/pricing?success=1")


def test_a_customer_coming_back_from_an_old_checkout_is_told_it_worked(client):
    """The whole point of the line above, followed through to what they see."""
    with app.app_context():
        db.session.add(Clinician(
            id="clin-9", provider="google", provider_subject="clin-9",
            email="c9@example.com", role=roles.PSYCHOTHERAPIST,
            created_at=datetime.now(timezone.utc), plan="paid",
            subscription_status="active"))
        db.session.commit()
    with client.session_transaction() as s:
        s["clinician_id"] = "clin-9"
        s["user_id"] = "clin-9"
    body = client.get("/billing?success=1", follow_redirects=True).get_data(as_text=True)
    assert "You're subscribed" in body


def test_the_page_and_the_link_agree(client):
    """A nav saying Pricing that opens a page titled "Plans & billing" reads as a
    broken link."""
    html = client.get("/pricing").get_data(as_text=True)
    assert "Pricing" in html
    assert "Plans &amp; billing" not in html
    assert "Plans & billing" not in html


def test_nothing_still_points_at_the_old_page_address():
    """The rename is only done if no link, script or email builds /billing by hand.
    url_for cannot catch these, so they are checked as text.

    The POST endpoints are excluded on purpose: /billing/checkout, /billing/portal
    and /billing/topup are form actions nobody sees or types, and moving them
    would be risk with no benefit.
    """
    checked = []
    for folder, names in (("templates", None), ("static/js", None)):
        base = os.path.join(ROOT, folder)
        for name in sorted(os.listdir(base)):
            if name.endswith((".html", ".js")):
                checked.append(os.path.join(base, name))
    checked.append(os.path.join(ROOT, "TogetherMindsAI.py"))

    offenders = []
    for path in checked:
        for num, line in enumerate(open(path, encoding="utf-8"), 1):
            for hit in re.findall(r'["\']([^"\']*?/billing)(?=["\'/])', line):
                offenders.append("%s:%d %s" % (os.path.basename(path), num, line.strip()[:90]))
    assert not offenders, "still building the old /billing page address:\n" + "\n".join(offenders)
