#!/usr/bin/env python3
"""
simulate_solo_chat.py
=====================
Opens a real Chromium browser, starts a Solo Reflection session on
TogetherMindsAI, sets a friendly display name, has a multi-turn
conversation with the AI, then downloads the transcript as a DOCX file.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    python scripts/simulate_solo_chat.py
    python scripts/simulate_solo_chat.py --url https://192.168.1.88:5001
    python scripts/simulate_solo_chat.py --headless
"""

import argparse
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

# ---------------------------------------------------------------------------
# Configuration — edit these as needed
# ---------------------------------------------------------------------------

BASE_URL     = "https://localhost:5001"
DISPLAY_NAME = "Alex"
DOWNLOAD_DIR = Path("downloads")

MESSAGES = [
    "Hi, I've been feeling quite anxious lately about work.",
    "My manager keeps piling on more tasks and I feel overwhelmed most days.",
    "I haven't been sleeping well either — maybe five hours a night.",
    "I used to enjoy my job but lately I dread Monday mornings.",
    "What are some practical things I can do to manage this stress?",
    "I like the idea of setting boundaries. How do I start that conversation with my manager?",
    "That's really helpful. I'll try that this week.",
    "One more thing — do you have any quick techniques for when anxiety spikes during the day?",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_ai_bubbles(page) -> int:
    return page.locator(".bubble-ai").count()


def _last_ai_text(page) -> str:
    bubbles = page.locator(".bubble-ai")
    if bubbles.count() == 0:
        return ""
    return bubbles.last.inner_text().strip()


def _wait_for_new_ai_reply(page, count_before: int, timeout_ms: int = 45_000):
    """Wait until the number of AI bubbles increases beyond count_before."""
    try:
        page.wait_for_function(
            f"document.querySelectorAll('.bubble-ai').length > {count_before}",
            timeout=timeout_ms,
        )
    except PWTimeout:
        print("  [warn] timed out waiting for AI reply — continuing anyway")


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------

def run(base_url: str, headless: bool):
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=200)
        context = browser.new_context(
            ignore_https_errors=True,   # self-signed TLS cert
            accept_downloads=True,
        )
        page = context.new_page()

        # ── Step 1: Auth page — tick consent boxes and continue ──────────────
        print(f"\n{'='*60}")
        print(f"  TogetherMindsAI Solo Chat Simulation")
        print(f"  Target: {base_url}")
        print(f"{'='*60}\n")

        print("[ 1/5 ] Opening auth page…")
        page.goto(f"{base_url}/auth/solo", wait_until="domcontentloaded")

        # Tick the three consent checkboxes
        for checkbox_id in ("#ageCheck", "#aiCheck", "#dataCheck"):
            page.locator(checkbox_id).check()

        # Wait for the continue button to become enabled (disabled attr removed)
        continue_btn = page.locator("#continueBtn")
        page.wait_for_function(
            "!document.getElementById('continueBtn').disabled",
            timeout=5_000,
        )
        continue_btn.click()

        # ── Step 2: Wait for redirect to therapy page ────────────────────────
        print("[ 2/5 ] Waiting for session to start…")
        try:
            page.wait_for_url("**/therapy/solo/**", timeout=20_000)
        except PWTimeout:
            # history.replaceState may have already masked the URL to /
            # Check JS globals instead
            pass

        # Read session/user IDs from the page's JS globals
        session_id = page.evaluate("typeof SESSION_ID !== 'undefined' ? SESSION_ID : null")
        user_id    = page.evaluate("typeof USER_ID    !== 'undefined' ? USER_ID    : null")

        if not session_id:
            sys.exit("Could not read SESSION_ID from page. Is the server running?")

        print(f"         Session ID : {session_id}")
        print(f"         User ID    : {user_id[:8]}…")

        # ── Step 3: Set display name ─────────────────────────────────────────
        print(f"[ 3/5 ] Setting display name to '{DISPLAY_NAME}'…")
        modal = page.locator("#displayNameModal")
        try:
            modal.wait_for(state="visible", timeout=8_000)
            inp = page.locator("#displayNameInput")
            inp.clear()
            inp.fill(DISPLAY_NAME)
            page.locator("#displayNameConfirmBtn").click()
            modal.wait_for(state="hidden", timeout=5_000)
            print(f"         Display name set: {session_id}-{DISPLAY_NAME}")
        except PWTimeout:
            print("         [warn] Display name modal did not appear — skipping")

        # ── Step 4: Chat loop ────────────────────────────────────────────────
        print(f"[ 4/5 ] Starting conversation ({len(MESSAGES)} messages)…\n")

        for i, msg in enumerate(MESSAGES, 1):
            label = f"  [{i:02d}/{len(MESSAGES):02d}]"
            print(f"{label} You : {msg}")

            ai_count_before = _count_ai_bubbles(page)

            # Fill and submit the message form
            page.locator("#messageInput").fill(msg)
            page.locator("#sendBtn").click()

            # Solo mode reloads the page on submit — wait for the page to settle
            # and for the new AI bubble to appear
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PWTimeout:
                pass

            _wait_for_new_ai_reply(page, ai_count_before)

            reply = _last_ai_text(page)
            # Trim long replies for console readability
            display_reply = reply[:120] + ("…" if len(reply) > 120 else "")
            print(f"{label} AI  : {display_reply}\n")

            time.sleep(0.3)

        # ── Step 5: Download DOCX transcript ─────────────────────────────────
        print("[ 5/5 ] Downloading transcript as DOCX…")
        with page.expect_download(timeout=20_000) as dl_info:
            page.goto(f"{base_url}/transcript/{session_id}/docx")
        download = dl_info.value
        dest = DOWNLOAD_DIR / download.suggested_filename
        download.save_as(str(dest))
        print(f"         Saved to : {dest.resolve()}\n")

        print("="*60)
        print("  Simulation complete.")
        print("="*60)

        if not headless:
            input("\nPress Enter to close the browser… ")

        browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate a TogetherMindsAI solo chat session.")
    parser.add_argument(
        "--url",
        default=BASE_URL,
        help=f"Base URL of the running app (default: {BASE_URL})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible browser window",
    )
    args = parser.parse_args()
    run(args.url, args.headless)
