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
# The header: the name on the left, the account on the right, no nav links
# ---------------------------------------------------------------------------

def test_the_header_has_no_navigation_links(client):
    """Baby cam: Home, My sessions, Pricing and the two sign-in links are gone."""
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert nav
    assert 'class="nav-link"' not in nav
    for gone in ("Home", "My sessions", "Pricing", "Clinician sign-in",
                 "Client sign-in", "navbar-toggler"):
        assert gone not in nav, gone


def test_the_header_name_is_babycamsecure(client):
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert "BabyCamSecure" in nav
    assert "TogetherMindsAI" not in nav


def test_a_signed_in_account_sees_its_menu_with_sign_out(client):
    with app.app_context():
        _clinician()
    with client.session_transaction() as s:
        s["clinician_id"] = "clin-1"
        s["user_id"] = "clin-1"
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert 'id="acctMenu"' in nav
    assert "c@example.com" in nav
    assert "/logout" in nav
    assert "Sign out" in nav


def test_a_signed_in_client_sees_its_menu_with_sign_out(client):
    with app.app_context():
        _client_account()
    with client.session_transaction() as s:
        s["client_account_id"] = "cli-1"
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert 'id="acctMenu"' in nav
    assert "/logout" in nav


def test_a_visitor_sees_no_account_menu(client):
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert 'id="acctMenu"' not in nav


def test_the_brand_still_links_home(client):
    """The live session hooks the brand link and Sign out to ask before leaving."""
    nav = _nav(client.get("/welcome").get_data(as_text=True))
    assert 'href="/"' in nav


def test_every_page_that_uses_the_header_shows_the_new_one(client):
    asserted = []
    for path in ("/welcome", "/client/login", "/login", "/pricing", "/privacy",
                 "/tos"):
        rv = client.get(path, follow_redirects=True)
        if rv.status_code != 200:
            continue
        html = rv.get_data(as_text=True)
        if "<nav" not in html:
            continue
        assert "BabyCamSecure" in _nav(html), path
        asserted.append(path)
    assert len(asserted) == 6, asserted


def test_pricing_is_still_reachable_from_the_start_page(client):
    """It left the header, so the start page is where a signed-in account finds it."""
    with app.app_context():
        _clinician()
    with client.session_transaction() as s:
        s["clinician_id"] = "clin-1"
        s["user_id"] = "clin-1"
    assert "/pricing" in client.get("/therapist").get_data(as_text=True)


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
