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
import random
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
DOWNLOAD_DIR = Path("downloads")

NAMES = [
    "Aisha", "Marcus", "Priya", "Darius", "Yuki",
    "Fatima", "Andre", "Soren", "Lucia", "Omar",
    "Mei", "Tariq", "Kofi", "Amara", "Ravi",
]

# Each scenario is a list of 10 messages following a realistic emotional arc
SCENARIOS = [
    # Work stress / burnout
    [
        "Hi, I've been feeling really anxious about work lately.",
        "My manager keeps piling on tasks and I feel overwhelmed most days.",
        "I haven't been sleeping well either — maybe five hours a night.",
        "I used to enjoy my job but now I dread Monday mornings.",
        "What are some practical things I can do to manage this stress?",
        "I like the idea of setting limits. How do I start that conversation with my manager?",
        "That's really helpful. I'll try that this week.",
        "One more thing — do you have quick techniques for when anxiety spikes during the day?",
        "The breathing thing sounds manageable. I'll try it.",
        "Thank you — this has been really helpful. I feel a bit lighter.",
    ],
    # Relationship grief / loneliness
    [
        "I'm not sure why I'm here. I just feel really alone lately.",
        "I broke up with someone three months ago and I still can't stop thinking about them.",
        "Everyone keeps telling me I should be over it by now. That makes it worse.",
        "I find myself replaying conversations, wondering what I could have done differently.",
        "The hardest part is the silence. Coming home to an empty flat.",
        "I know I need to move on but I don't even know what that looks like.",
        "It's funny — I was the one who ended things. But I'm still devastated.",
        "I think I'm scared I made the wrong choice.",
        "Hearing you say that helps. I've been carrying a lot of guilt.",
        "I think I just needed to say all that out loud. Thank you.",
    ],
    # Anxiety / self-doubt
    [
        "I've been struggling with self-doubt for a long time.",
        "I constantly feel like I'm not good enough — at work, in relationships, everywhere.",
        "Even when things go well I find a reason to dismiss it. Like I got lucky.",
        "My friends say I'm too hard on myself but I don't know how to stop.",
        "I think it started when I was pretty young. My parents had high expectations.",
        "I don't blame them. But I still carry that voice telling me I'm not enough.",
        "It's exhausting. I'm tired of fighting my own brain.",
        "I've tried journaling but I end up writing a list of everything I've failed at.",
        "I never thought of it that way. That's actually something to sit with.",
        "I think I needed permission to be imperfect. This helped more than I expected.",
    ],
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

    display_name = random.choice(NAMES)
    messages     = random.choice(SCENARIOS)

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
        print(f"  Name     : {display_name}")
        print(f"  Scenario : {messages[0][:60]}…")
        print(f"  Target   : {base_url}")
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
        print(f"[ 3/5 ] Setting display name to '{display_name}'…")
        modal = page.locator("#displayNameModal")
        try:
            # 20s timeout — on_join generates the opening message via Claude
            # before emitting history, which can take 5-8s on a slow API call.
            modal.wait_for(state="visible", timeout=20_000)
            inp = page.locator("#displayNameInput")
            inp.clear()
            inp.fill(display_name)
            page.locator("#displayNameConfirmBtn").click()
            modal.wait_for(state="hidden", timeout=5_000)
            print(f"         Display name set: {session_id}-{display_name}")
        except PWTimeout:
            print("         [warn] Display name modal did not appear — skipping")

        # ── Step 4: Chat loop ────────────────────────────────────────────────
        print(f"[ 4/5 ] Starting conversation ({len(messages)} messages)…\n")

        for i, msg in enumerate(messages, 1):
            label = f"  [{i:02d}/{len(messages):02d}]"
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

        # ── Step 5: Download transcripts (DOCX + PDF) ────────────────────────
        print("[ 5/5 ] Downloading transcripts…")
        for fmt in ("docx", "pdf"):
            url = f"{base_url}/transcript/{session_id}/{fmt}"
            with page.expect_download(timeout=30_000) as dl_info:
                # Use evaluate to navigate — page.goto raises an error when a
                # download starts instead of a normal page load
                page.evaluate(f"window.location.href = '{url}'")
            download = dl_info.value
            dest = DOWNLOAD_DIR / download.suggested_filename
            download.save_as(str(dest))
            print(f"         {fmt.upper()} saved to : {dest.resolve()}")

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
