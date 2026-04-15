#!/usr/bin/env python3
"""
simulate_couple_chat.py
=======================
Opens Chromium (Partner 1) and Firefox (Partner 2) simultaneously.
Each partner is an LLM persona powered by the Claude API — messages are
generated in context, not pulled from a static pool.

Turn taking is probabilistic: either partner can speak once, twice, or
three times in a row; ~20 % chance of "interrupting" before the AI replies.

Requirements:
    pip install playwright anthropic python-dotenv
    playwright install chromium firefox

Usage:
    python scripts/simulate_couple_chat.py
    python scripts/simulate_couple_chat.py --url https://192.168.1.88:5001
    python scripts/simulate_couple_chat.py --turns 20 --headless
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — env var can be set directly

try:
    import anthropic
except ImportError:
    sys.exit("anthropic is not installed. Run: pip install anthropic")

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright is not installed. Run: pip install playwright && playwright install chromium firefox")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL     = "https://localhost:5001"
DOWNLOAD_DIR = Path("downloads")
NUM_TURNS    = 18          # total "speak turns" across both partners
LLM_MODEL    = "claude-haiku-4-5-20251001"   # fast + cheap for simulation

NAMES = [
    "Aisha", "Marcus", "Priya", "Darius",
    "Yuki", "Fatima", "Andre", "Soren",
    "Lucia", "Omar", "Mei", "Tariq",
    "Ingrid", "Kofi", "Amara", "Ravi",
    "Elena", "Jamal", "Nadia", "Diego",
]

# Short reactive phrases used for interrupts (no LLM call needed — fires fast)
INTERRUPT_PHRASES = [
    "That's not what I said.",
    "Fine.",
    "Can we just stop for a second?",
    "I can't believe you're bringing that up again.",
    "You always do this.",
    "I'm listening, go on.",
    "Sorry — keep going.",
    "Okay, okay.",
    "I just need a moment.",
    "That actually hurt.",
]

# ---------------------------------------------------------------------------
# LLM persona builder
# ---------------------------------------------------------------------------

def _build_persona(my_name: str, partner_name: str, is_first: bool) -> str:
    if is_first:
        return f"""You are {my_name}. Your partner in this couple's therapy session is {partner_name}.

CRITICAL name rules — follow these exactly:
- Your name is {my_name}. Speak as {my_name} in first person ("I feel...", "I think...").
- Your partner's name is {partner_name}. When referring to your partner, always use "{partner_name}".
- Never refer to yourself in third person. Never use your own name ({my_name}) to refer to someone else.

Your emotional profile:
- You tend to withdraw and get defensive when things get heated.
- You love {partner_name} but struggle to say it directly under pressure.
- You feel blamed and misunderstood a lot of the time.
- You sometimes go quiet or give short clipped answers.
- Occasionally you open up with something vulnerable and honest.

Rules:
- You are NOT a therapist. You are a person in therapy.
- Speak naturally — messy, emotional, real. No bullet points, no neat advice.
- Vary your length: sometimes one short sentence, sometimes two or three.
- ALWAYS react to the most recent thing said — by the AI guide or by {partner_name}.
  Do NOT restate something you have already expressed. Move the conversation forward.
- Don't be a pushover but don't be a villain either — you're a flawed but loving person."""
    else:
        return f"""You are {my_name}. Your partner in this couple's therapy session is {partner_name}.

CRITICAL name rules — follow these exactly:
- Your name is {my_name}. Speak as {my_name} in first person ("I feel...", "I think...").
- Your partner's name is {partner_name}. When referring to your partner, always use "{partner_name}".
- Never refer to yourself in third person. Never use your own name ({my_name}) to refer to someone else.

Your emotional profile:
- You are the "pursuing" partner — you crave more emotional connection.
- You feel unheard and sometimes push too hard trying to get through.
- You over-explain when anxious, which makes things worse.
- You genuinely want to fix things but don't always know how.
- When {partner_name} goes quiet, you escalate — then regret it.

Rules:
- You are NOT a therapist. You are a person in therapy.
- Speak naturally — raw, sometimes messy. No neat summaries or bullet points.
- Vary your length: sometimes a sharp frustrated line, sometimes a longer plea.
- ALWAYS react to the most recent thing said — by the AI guide or by {partner_name}.
  Do NOT restate something you have already expressed. Move the conversation forward.
- Don't be a villain — you're scared of losing the relationship."""


# ---------------------------------------------------------------------------
# LLM message generation
# ---------------------------------------------------------------------------

async def generate_messages(
    client: anthropic.AsyncAnthropic,
    persona: str,
    history_text: str,
    burst: int,
) -> list[str]:
    """Ask the LLM to generate `burst` messages for this partner."""
    prompt = f"""The therapy session so far:

{history_text if history_text.strip() else "(The session has just started — you haven't spoken yet.)"}

Generate exactly {burst} message(s) that your character would send next.
Respond ONLY with valid JSON in this exact format — no other text:
{{"messages": ["message one", "message two"]}}

Each message: 1–3 sentences, natural conversational tone, emotionally authentic."""

    try:
        response = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=300,
            system=persona,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        msgs = data.get("messages", [])
        return [m.strip() for m in msgs if m.strip()][:burst]
    except Exception as exc:
        print(f"  [warn] LLM error ({exc}) — using fallback phrase")
        return [random.choice(INTERRUPT_PHRASES)]


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def _scrape_history(page) -> str:
    """Read all visible chat messages from the page as plain text."""
    try:
        entries = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.bubble-ai, .bubble-user').forEach(el => {
                const sender = el.querySelector('.bubble-sender');
                const body   = el.querySelector('p');
                if (sender && body) {
                    out.push(sender.innerText.trim() + ': ' + body.innerText.trim());
                }
            });
            return out;
        }""")
        return "\n".join(entries)
    except Exception:
        return ""


async def _count_bubbles(page) -> int:
    try:
        return await page.locator(".bubble-ai, .bubble-user").count()
    except Exception:
        return 0


async def _wait_for_new_message(page, count_before: int, timeout_ms: int = 30_000):
    try:
        await page.wait_for_function(
            f"document.querySelectorAll('.bubble-ai, .bubble-user').length > {count_before}",
            timeout=timeout_ms,
        )
    except PWTimeout:
        pass  # continue even if no new message arrived


async def _dismiss_modal_if_present(page, name: str):
    """Dismiss the display name modal if it is currently blocking the UI.

    This can happen on Socket.IO reconnects (each reconnect re-emits 'join'
    which triggers the history handler and may re-show the prompt).
    """
    modal = page.locator("#displayNameModal")
    try:
        if await modal.is_visible(timeout=500):
            inp = page.locator("#displayNameInput")
            current = await inp.input_value()
            if not current.strip():
                await inp.fill(name)
            await page.locator("#displayNameConfirmBtn").click()
            await modal.wait_for(state="hidden", timeout=5_000)
    except Exception:
        pass  # modal not present or already hidden — continue


async def _send_message(page, text: str, name: str = ""):
    """Type and send a message via the Socket.IO input.

    Waits for the send button to be enabled (it starts disabled until the AI
    opening message arrives), then dismisses any blocking modal, then sends.
    """
    await page.wait_for_function(
        "!document.getElementById('sendBtn').disabled",
        timeout=30_000,
    )
    await _dismiss_modal_if_present(page, name)
    inp = page.locator("#messageInput")
    await inp.fill(text)
    await page.locator("#sendBtn").click()


# ---------------------------------------------------------------------------
# Partner coroutine
# ---------------------------------------------------------------------------

async def run_partner(
    *,
    browser_type,           # pw.chromium or pw.firefox
    name: str,
    partner_name: str,
    is_creator: bool,
    session_queue: asyncio.Queue,
    ready_event: asyncio.Event,
    turn_queue: asyncio.Queue,  # receives "go" signals with burst count
    done_event: asyncio.Event,
    base_url: str,
    headless: bool,
    llm_client: anthropic.AsyncAnthropic,
    persona: str,
):
    browser_name = "Chromium" if is_creator else "Firefox"
    label        = f"[{name}/{browser_name}]"

    browser = await browser_type.launch(headless=headless, slow_mo=150)
    context = await browser.new_context(
        ignore_https_errors=True,
        accept_downloads=True,
    )
    page = await context.new_page()

    try:
        # ── Auth ─────────────────────────────────────────────────────────────
        if is_creator:
            print(f"{label} Opening auth page…")
            await page.goto(f"{base_url}/auth/couple", wait_until="domcontentloaded")
        else:
            session_id = await session_queue.get()
            print(f"{label} Joining session {session_id}…")
            # POST to /session/join to set the pending_couple_session cookie
            await page.goto(f"{base_url}/session/join", wait_until="domcontentloaded")
            await page.locator("#session_id").fill(session_id)
            await page.locator("button[type=submit]").click()
            # Should redirect to /auth/couple
            await page.wait_for_url("**/auth/couple**", timeout=10_000)

        # Tick consent boxes and click Continue
        for cb in ("#ageCheck", "#aiCheck", "#dataCheck"):
            await page.locator(cb).check()
        await page.wait_for_function(
            "!document.getElementById('continueBtn').disabled", timeout=5_000
        )
        await page.locator("#continueBtn").click()

        # Wait for therapy page
        try:
            await page.wait_for_url("**/therapy/couple/**", timeout=20_000)
        except PWTimeout:
            pass  # history.replaceState may have already masked the URL

        session_id = await page.evaluate(
            "typeof SESSION_ID !== 'undefined' ? SESSION_ID : null"
        )
        user_id = await page.evaluate(
            "typeof USER_ID !== 'undefined' ? USER_ID : null"
        )

        if not session_id:
            print(f"{label} ERROR: Could not read SESSION_ID")
            return

        print(f"{label} Session: {session_id}  User: {user_id[:8]}…")

        if is_creator:
            await session_queue.put(session_id)

        # ── Display name ─────────────────────────────────────────────────────
        modal = page.locator("#displayNameModal")
        try:
            # Creator waits longer — the server generates an opening message via
            # Claude before emitting 'history', which can take 5-8 s on slow API.
            modal_timeout = 20_000 if is_creator else 8_000
            await modal.wait_for(state="visible", timeout=modal_timeout)
            await page.locator("#displayNameInput").fill(name)
            await page.locator("#displayNameConfirmBtn").click()
            await modal.wait_for(state="hidden", timeout=5_000)
            print(f"{label} Display name set: {session_id}-{name}")
        except PWTimeout:
            print(f"{label} [warn] Display name modal did not appear")

        # ── Signal ready and wait for partner ────────────────────────────────
        ready_event.set() if not ready_event.is_set() else None
        # Small pause to let partner settle
        await asyncio.sleep(2)

        # ── Turn loop ────────────────────────────────────────────────────────
        while not done_event.is_set():
            try:
                burst = await asyncio.wait_for(turn_queue.get(), timeout=60)
            except asyncio.TimeoutError:
                break

            if burst == 0:  # sentinel — simulation over
                break

            history = await _scrape_history(page)
            messages = await generate_messages(llm_client, persona, history, burst)

            for i, msg in enumerate(messages):
                count_before = await _count_bubbles(page)
                print(f"  {label} ({i+1}/{len(messages)}): {msg[:90]}{'…' if len(msg)>90 else ''}")
                await _send_message(page, msg, name=name)

                # random typing pause between burst messages
                if i < len(messages) - 1:
                    await asyncio.sleep(random.uniform(0.8, 2.5))

            # After burst: maybe wait for a reply, maybe interrupt (20 % chance)
            interrupt = random.random() < 0.20
            if not interrupt:
                count_after = await _count_bubbles(page)
                await _wait_for_new_message(page, count_after, timeout_ms=20_000)
            else:
                await asyncio.sleep(random.uniform(0.5, 1.5))

        # ── Download transcripts ─────────────────────────────────────────────
        if is_creator:
            print(f"\n{label} Downloading transcripts…")
            for fmt in ("docx", "pdf"):
                url = f"{base_url}/transcript/{session_id}/{fmt}"
                async with page.expect_download(timeout=30_000) as dl_info:
                    await page.evaluate(f"window.location.href = '{url}'")
                download = await dl_info.value
                dest = DOWNLOAD_DIR / download.suggested_filename
                await download.save_as(str(dest))
                print(f"  {label} {fmt.upper()} saved: {dest.resolve()}")

    finally:
        if not headless:
            await asyncio.sleep(3)
        await browser.close()


# ---------------------------------------------------------------------------
# Turn scheduler
# ---------------------------------------------------------------------------

async def schedule_turns(
    q1: asyncio.Queue,
    q2: asyncio.Queue,
    done_event: asyncio.Event,
    num_turns: int,
):
    """
    Distribute turns between partners keeping both roughly balanced.

    Dynamic rebalancing: if one partner is 2+ turns ahead, heavily favour
    the other. This prevents one partner dominating while still allowing
    natural short runs of 2 turns in a row.
    """
    await asyncio.sleep(4)  # wait for both partners to set display names

    p1_turns = 0
    p2_turns = 0

    for turn in range(num_turns):
        # Rebalance: if one partner is 2+ turns ahead, strongly favour the other
        diff = p1_turns - p2_turns
        if diff >= 2:
            weights = [0.15, 0.85]
        elif diff <= -2:
            weights = [0.85, 0.15]
        else:
            # Small natural variation around 50/50
            weights = [0.45, 0.55] if turn % 2 == 0 else [0.55, 0.45]

        who = random.choices([0, 1], weights=weights)[0]
        burst = random.choices([1, 2, 3], weights=[0.62, 0.28, 0.10])[0]

        if who == 0:
            p1_turns += 1
            await q1.put(burst)
        else:
            p2_turns += 1
            await q2.put(burst)

        # Pause between turns — shorter early on (heated), longer later (settling)
        progress = turn / max(num_turns - 1, 1)
        lo = 1.5 + progress * 2.0
        hi = 4.0 + progress * 4.0
        await asyncio.sleep(random.uniform(lo, hi))

    # Send sentinels to end both loops
    await q1.put(0)
    await q2.put(0)
    done_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(base_url: str, num_turns: int, headless: bool):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to your .env file.")

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    name1, name2 = random.sample(NAMES, 2)
    persona1 = _build_persona(name1, name2, is_first=True)
    persona2 = _build_persona(name2, name1, is_first=False)

    llm = anthropic.AsyncAnthropic(api_key=api_key)

    print(f"\n{'='*60}")
    print(f"  TogetherMindsAI Couple Chat Simulation")
    print(f"  Partner 1 (Chromium) : {name1}")
    print(f"  Partner 2 (Firefox)  : {name2}")
    print(f"  Turns                : {num_turns}")
    print(f"  Target               : {base_url}")
    print(f"{'='*60}\n")

    session_queue = asyncio.Queue()
    ready_event   = asyncio.Event()
    done_event    = asyncio.Event()
    turn_q1       = asyncio.Queue()
    turn_q2       = asyncio.Queue()

    async with async_playwright() as pw:
        await asyncio.gather(
            run_partner(
                browser_type=pw.chromium,
                name=name1,
                partner_name=name2,
                is_creator=True,
                session_queue=session_queue,
                ready_event=ready_event,
                turn_queue=turn_q1,
                done_event=done_event,
                base_url=base_url,
                headless=headless,
                llm_client=llm,
                persona=persona1,
            ),
            run_partner(
                browser_type=pw.firefox,
                name=name2,
                partner_name=name1,
                is_creator=False,
                session_queue=session_queue,
                ready_event=ready_event,
                turn_queue=turn_q2,
                done_event=done_event,
                base_url=base_url,
                headless=headless,
                llm_client=llm,
                persona=persona2,
            ),
            schedule_turns(turn_q1, turn_q2, done_event, num_turns),
        )

    print(f"\n{'='*60}")
    print("  Simulation complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate a TogetherMindsAI couple therapy session with LLM-driven partners."
    )
    parser.add_argument("--url",     default=BASE_URL,   help=f"App base URL (default: {BASE_URL})")
    parser.add_argument("--turns",   default=NUM_TURNS,  type=int, help=f"Total speak turns (default: {NUM_TURNS})")
    parser.add_argument("--headless", action="store_true", help="Run without visible browser windows")
    args = parser.parse_args()

    asyncio.run(main(args.url, args.turns, args.headless))
