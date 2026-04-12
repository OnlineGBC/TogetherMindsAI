"""
tests/test_browser.py
---------------------
Playwright browser tests — verify JavaScript-dependent behaviour that
pytest's HTTP test client cannot exercise:

  - Full auth flow (keypair generation → challenge → ECDSA verify)
  - Auth error fallback (Invalid signature / User not found → re-register)
  - Session ID display and nickname field
  - End Session modal
  - Sending a message and receiving an AI response
  - Link targets (befrienders.org)

Run alongside all other tests with: pytest tests/
Run in isolation with:            pytest tests/test_browser.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest


pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _go(page, live_server_url, path):
    page.goto(f"{live_server_url}{path}")


def _complete_auth(page, live_server_url, mode="solo"):
    """Navigate to auth page, check all boxes, click Continue, wait for redirect."""
    _go(page, live_server_url, f"/auth/{mode}")
    for cb in page.locator(".consent-check").all():
        cb.check()
    page.locator("#continueBtn").click()
    # Wait until redirected away from /auth/
    page.wait_for_url(lambda url: "/auth/" not in url, timeout=10_000)


# ---------------------------------------------------------------------------
# 1. Home page
# ---------------------------------------------------------------------------

def test_home_page_loads(page, live_server_url):
    _go(page, live_server_url, "/")
    assert "TogetherMindsAI" in page.title()


# ---------------------------------------------------------------------------
# 2. Auth page loads for solo mode
# ---------------------------------------------------------------------------

def test_auth_solo_page_loads(page, live_server_url):
    _go(page, live_server_url, "/auth/solo")
    assert page.locator(".consent-check").count() > 0


# ---------------------------------------------------------------------------
# 3. Consent checkboxes gate the Continue button
# ---------------------------------------------------------------------------

def test_continue_button_disabled_until_all_checked(page, live_server_url):
    _go(page, live_server_url, "/auth/solo")
    btn = page.locator("#continueBtn")
    assert btn.is_disabled()

    checks = page.locator(".consent-check").all()
    for i, cb in enumerate(checks):
        cb.check()
        if i < len(checks) - 1:
            assert btn.is_disabled(), f"Button should still be disabled after {i+1} checks"

    assert not btn.is_disabled(), "Button should be enabled after all boxes checked"


# ---------------------------------------------------------------------------
# 4. Full auth flow → lands on solo therapy page
# ---------------------------------------------------------------------------

def test_full_auth_flow_solo(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")
    assert "/therapy/solo/" in page.url


# ---------------------------------------------------------------------------
# 5. Therapy page shows session ID
# ---------------------------------------------------------------------------

def test_therapy_page_shows_session_id(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")
    session_id_el = page.locator("text=/Session ID/")
    assert session_id_el.count() > 0


# ---------------------------------------------------------------------------
# 6. Sending a message shows an AI response
# ---------------------------------------------------------------------------

def test_sending_message_shows_ai_response(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")

    page.locator("textarea, input[name='message']").fill("I feel anxious today")
    page.locator("button[type='submit'], [data-send], .send-btn, form button").last.click()

    # Wait for AI response to appear (mocked reply contains "therapist")
    page.wait_for_selector("text=therapist", timeout=10_000)


# ---------------------------------------------------------------------------
# 7. Invalid signature / stale identity → silently re-registers, no error shown
# ---------------------------------------------------------------------------

def test_stale_identity_rereg_no_error(page, live_server_url):
    """Simulate a stale user_id in sessionStorage by injecting an unknown ID,
    then verifying auth succeeds without showing an error to the user."""
    _go(page, live_server_url, "/auth/solo")

    # Inject a user_id that doesn't exist in the DB
    page.evaluate("""() => {
        sessionStorage.setItem('user_id', 'nonexistent-user-id-1234');
        sessionStorage.setItem('therapy_mode', 'solo');
    }""")

    for cb in page.locator(".consent-check").all():
        cb.check()
    page.locator("#continueBtn").click()

    # Should redirect to therapy page without showing an error
    page.wait_for_url(lambda url: "/therapy/solo/" in url, timeout=10_000)
    assert page.locator("#authError").is_hidden()


# ---------------------------------------------------------------------------
# 8. End Session modal opens and displays session ID
# ---------------------------------------------------------------------------

def test_end_session_modal_shows_session_id(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")

    # Open the End Session modal
    page.locator("text=End Session").first.click()
    page.wait_for_selector("#endSessionModal.show", timeout=5_000)

    session_id_text = page.locator("#endSessionIdDisplay").inner_text()
    assert len(session_id_text) > 0


# ---------------------------------------------------------------------------
# 9. Nickname field saves to localStorage
# ---------------------------------------------------------------------------

def test_nickname_saved_to_localstorage(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")

    page.locator("text=End Session").first.click()
    page.wait_for_selector("#endSessionModal.show", timeout=5_000)

    page.locator("#endSessionNickname").fill("My Monday session")

    saved = page.evaluate("""() => {
        for (var k in localStorage) {
            if (k.startsWith('session_nickname_')) return localStorage[k];
        }
        return null;
    }""")
    assert saved == "My Monday session"


# ---------------------------------------------------------------------------
# 10. Copy button is present in the modal
# ---------------------------------------------------------------------------

def test_copy_button_present_in_modal(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")
    page.locator("text=End Session").first.click()
    page.wait_for_selector("#endSessionModal.show", timeout=5_000)
    assert page.locator("#endSessionCopyBtn").is_visible()


# ---------------------------------------------------------------------------
# 11. Disclaimer bar contains befrienders.org link
# ---------------------------------------------------------------------------

def test_disclaimer_bar_has_befrienders_link(page, live_server_url):
    _go(page, live_server_url, "/")
    link = page.locator(".disclaimer-bar a[href*='befrienders.org']")
    assert link.count() > 0


# ---------------------------------------------------------------------------
# 12. befrienders.org link points to /find-support-now
# ---------------------------------------------------------------------------

def test_befrienders_link_points_to_find_support_now(page, live_server_url):
    _go(page, live_server_url, "/")
    links = page.locator("a[href*='befrienders.org']").all()
    for link in links:
        href = link.get_attribute("href")
        assert "find-support-now" in href, f"Unexpected href: {href}"
