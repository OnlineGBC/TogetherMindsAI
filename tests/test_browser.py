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
  - Multi-turn chat: 3 messages in solo, couple (2 users), group (4 users)

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
    """Navigate to auth page, check all boxes, click Continue, wait for redirect.

    Also patches window.io to use polling-only transport before any navigation.
    The werkzeug test server rejects WebSocket upgrades (400/500), and the
    resulting errors corrupt engineio's session tracking.  Forcing polling
    prevents those upgrade attempts across all auth flows.
    """
    _patch_socketio_polling(page)
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
    assert "/therapy/solo" in page.url


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
    page.wait_for_url(lambda url: "/therapy/solo" in url, timeout=10_000)
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


# ---------------------------------------------------------------------------
# 10. Copy button is present in the modal
# ---------------------------------------------------------------------------

def test_copy_button_present_in_modal(page, live_server_url):
    _complete_auth(page, live_server_url, mode="solo")
    page.locator("text=End Session").first.click()
    page.wait_for_selector("#endSessionModal.show", timeout=5_000)
    assert page.locator("#endSessionCopyBtn").is_visible()


# ---------------------------------------------------------------------------
# 11. End-session modal saves friendly label to localStorage
# ---------------------------------------------------------------------------

def test_label_saved_to_localstorage_via_modal(page, live_server_url):
    """Typing a label in the end-session modal must save it to localStorage.

    Friendly names are local-only — nothing is sent to the server.
    The label must be stored under the key session_nickname_<sessionId>.
    """
    _complete_auth(page, live_server_url, mode="solo")

    page.locator("text=End Session").first.click()
    page.wait_for_selector("#endSessionModal.show", timeout=5_000)

    page.locator("#endSessionNickname").fill("My Monday session")
    # Trigger the input event so localStorage is written
    page.locator("#endSessionNickname").dispatch_event("input")

    saved = page.evaluate("""() => {
        for (var i = 0; i < localStorage.length; i++) {
            var k = localStorage.key(i);
            if (k && k.startsWith('session_nickname_')) return localStorage.getItem(k);
        }
        return null;
    }""")
    assert saved == "My Monday session"


# ---------------------------------------------------------------------------
# 12. Join form substitutes a locally-saved label for the real session ID
# ---------------------------------------------------------------------------

def test_join_form_substitutes_local_label_for_session_id(page, live_server_url):
    """If the user types their local label into the join form, the JS must swap
    it for the real session ID before submitting so the server lookup succeeds.
    """
    _complete_auth(page, live_server_url, mode="solo")

    # Read the real session ID from the page URL
    current_url = page.url
    session_id = current_url.rstrip("/").split("/")[-1]

    # Plant the label in localStorage as initSessionNickname / the modal would
    page.evaluate(f"""() => {{
        localStorage.setItem('session_nickname_{session_id}', 'My Monday session');
    }}""")

    # Navigate to the join page and submit the label
    _go(page, live_server_url, "/session/join")
    page.locator("#session_id").fill("My Monday session")
    page.locator("button[type=submit]").click()

    # Server must redirect to the correct therapy page using the real session ID
    page.wait_for_url(lambda url: session_id in url, timeout=5_000)
    assert session_id in page.url


# ---------------------------------------------------------------------------
# 13. Disclaimer bar contains befrienders.org link
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


# ---------------------------------------------------------------------------
# 13. Solo — 3 chat messages
# ---------------------------------------------------------------------------

def test_solo_three_chat_messages(page, live_server_url):
    """Send 3 messages in a solo session and verify user text + AI replies appear."""
    _complete_auth(page, live_server_url, mode="solo")

    msgs = [
        "I feel anxious today",
        "I have been struggling with work",
        "How can I manage stress better?",
    ]
    for text in msgs:
        page.locator("#messageInput").fill(text)
        page.locator("button[type='submit']").click()
        page.wait_for_load_state("networkidle")

    chat = page.locator("#chatBox")
    for text in msgs:
        assert chat.locator(f"text={text}").count() > 0, f"Expected to find: {text}"
    # Opening message + 3 AI replies = at least 3 AI bubbles
    assert chat.locator(".bubble-ai").count() >= 3


# ---------------------------------------------------------------------------
# 14. Couple — 3 messages each for 2 users
# ---------------------------------------------------------------------------

def _join_existing_session(page, live_server_url, session_id, mode):
    """Go to /session/join, submit session_id, complete auth for the given mode."""
    _go(page, live_server_url, "/session/join")
    page.locator("#session_id").fill(session_id)
    page.locator("button[type='submit']").click()
    page.wait_for_url(lambda url: f"/auth/{mode}" in url, timeout=8_000)
    for cb in page.locator(".consent-check").all():
        cb.check()
    page.locator("#continueBtn").click()
    page.wait_for_url(lambda url: "/auth/" not in url, timeout=15_000)


def _patch_socketio_polling(page):
    """Inject an init script that forces socket.io to use polling only.

    The werkzeug test server rejects WebSocket upgrades (400/500) and the
    werkzeug error leaves socket.io in a broken state.  This must be called
    BEFORE any navigation that loads socket.io (i.e. before _complete_auth).
    It intercepts window.io the moment socket.io-client defines it, patching
    every subsequent io() call to use polling transport with no upgrade.
    """
    page.add_init_script("""
        (function () {
            var _real;
            Object.defineProperty(window, 'io', {
                configurable: true,
                enumerable: true,
                get: function () { return _real; },
                set: function (v) {
                    _real = function () {
                        var args = Array.prototype.slice.call(arguments);
                        // Find the options object (first plain object arg, no 'url' key)
                        var injected = false;
                        for (var i = 0; i < args.length; i++) {
                            if (args[i] && typeof args[i] === 'object' && !args[i].href) {
                                args[i].transports = ['polling'];
                                args[i].upgrade    = false;
                                injected = true;
                                break;
                            }
                        }
                        if (!injected) {
                            args.push({ transports: ['polling'], upgrade: false });
                        }
                        return v.apply(this, args);
                    };
                    for (var k in v) { try { _real[k] = v[k]; } catch (e) {} }
                }
            });
        })();
    """)


def _ensure_socketio_connected(page, timeout=15_000):
    """Wait for SocketIO to reach connected state."""
    page.wait_for_function("window.socket && window.socket.connected", timeout=timeout)


def _send_socketio(page, text):
    """Type text, click Send, and wait for a new AI bubble to confirm the reply arrived."""
    initial_ai = page.locator(".bubble-ai").count()
    page.locator("#messageInput").fill(text)
    page.locator("#sendBtn").click()
    page.wait_for_function(
        f"document.querySelectorAll('.bubble-ai').length > {initial_ai}",
        timeout=15_000,
    )


def _emit_as_user(page, session_id, user_id, text):
    """Emit a send_message event from the browser socket, spoofing a different user_id.

    This lets us simulate a second participant through the same socket connection,
    which avoids threading conflicts with the live server.  The server processes
    the message as if it came from user_id, rendering it as a partner bubble.
    """
    page.evaluate("""([sid, uid, txt]) => {
        window.socket.emit('send_message', {
            session_id: sid,
            user_id:    uid,
            text:       txt
        });
    }""", [session_id, user_id, text])


def test_couple_three_chats_each(page, live_server_url):
    """Two partners (same socket, different user_ids) each send 3 messages."""
    from urllib.parse import urlparse, parse_qs

    # --- User 1 (browser): create couple session ---
    _complete_auth(page, live_server_url, mode="couple")
    assert "/therapy/couple/" in page.url

    # Extract session_id: host URL is /therapy/couple/<user_id>[?session_id=...]
    parsed = urlparse(page.url)
    qs = parse_qs(parsed.query)
    session_id = qs["session_id"][0] if "session_id" in qs else parsed.path.rstrip("/").split("/")[-1]

    _ensure_socketio_connected(page)

    user2_id = "test-couple-user-2"

    msgs1 = ["Hello from partner one", "I feel stressed lately", "What can we try together?"]
    msgs2 = ["Hello from partner two", "I have trouble communicating", "How do we improve this?"]

    for m1, m2 in zip(msgs1, msgs2):
        # User 1 sends via the browser UI; wait for AI bubble
        _send_socketio(page, m1)
        # User 2 sends via socket.emit with a different user_id; wait for AI bubble
        initial_ai = page.locator(".bubble-ai").count()
        _emit_as_user(page, session_id, user2_id, m2)
        page.wait_for_function(
            f"document.querySelectorAll('.bubble-ai').length > {initial_ai}",
            timeout=15_000,
        )

    # Both users' messages should be visible in the chat
    chat = page.locator("#chatBox")
    for text in msgs1 + msgs2:
        assert chat.locator(f"text={text}").count() > 0, f"Expected to find: {text}"

    # AI should have replied to every message (6 total)
    assert page.locator(".bubble-ai").count() >= 6


# ---------------------------------------------------------------------------
# 15. Group — 3 messages each for 4 users
# ---------------------------------------------------------------------------

def test_group_three_chats_four_people(page, live_server_url):
    """Four group members (same socket, different user_ids) each send 3 messages."""
    from urllib.parse import urlparse

    # --- User 1 (browser): create group session ---
    _complete_auth(page, live_server_url, mode="group")
    assert "/therapy/group/" in page.url
    # Group URL: /therapy/group/<session_id>
    session_id = urlparse(page.url).path.rstrip("/").split("/")[-1]

    _ensure_socketio_connected(page)

    test_user_ids = [f"test-group-user-{i}" for i in range(2, 5)]

    user_msgs = [
        ["User one message one",   "User one message two",   "User one message three"],
        ["User two message one",   "User two message two",   "User two message three"],
        ["User three message one", "User three message two", "User three message three"],
        ["User four message one",  "User four message two",  "User four message three"],
    ]

    # Round-robin: User 1 via UI, Users 2-4 via socket.emit with spoofed user_ids
    for round_idx in range(3):
        _send_socketio(page, user_msgs[0][round_idx])
        for user_idx, uid in enumerate(test_user_ids):
            msg_text = user_msgs[user_idx + 1][round_idx]
            initial_ai = page.locator(".bubble-ai").count()
            _emit_as_user(page, session_id, uid, msg_text)
            page.wait_for_function(
                f"document.querySelectorAll('.bubble-ai').length > {initial_ai}",
                timeout=15_000,
            )

    # All messages from all four users should appear in the chat
    chat = page.locator("#chatBox")
    for msgs in user_msgs:
        for text in msgs:
            assert chat.locator(f"text={text}").count() > 0, f"Expected to find: {text}"

    # AI should have replied to every message (12 total)
    assert page.locator(".bubble-ai").count() >= 12
