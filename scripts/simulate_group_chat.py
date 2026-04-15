#!/usr/bin/env python3
"""
simulate_group_chat.py
======================
Opens N Chromium browser contexts simultaneously (default 4).
Each member is an LLM persona powered by the Claude API — messages are
generated in context, not pulled from a static pool.

Turn taking is probabilistic: any member can speak once, twice, or three
times in a row; ~20 % chance of "interrupting" before the AI replies.
Dynamic rebalancing keeps participation roughly equal across all members.

Requirements:
    pip install playwright anthropic python-dotenv
    playwright install chromium

Usage:
    python scripts/simulate_group_chat.py
    python scripts/simulate_group_chat.py --url https://192.168.1.88:5001
    python scripts/simulate_group_chat.py --members 5 --turns 24 --headless
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
    sys.exit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL     = "https://localhost:5001"
DOWNLOAD_DIR = Path("downloads")
NUM_TURNS    = 20          # total "speak turns" distributed across all members
NUM_MEMBERS  = 4           # number of group participants (2–6)
LLM_MODEL    = "claude-haiku-4-5-20251001"   # fast + cheap for simulation

NAMES = [
    "Alex", "Jamie", "Sam", "Morgan",
    "Riley", "Casey", "Jordan", "Quinn",
    "Avery", "Blake", "Reese", "Skyler",
]

# Short reactive phrases used for interrupts (no LLM call needed)
INTERRUPT_PHRASES = [
    "Yeah, I felt that.",
    "Sorry — keep going.",
    "That's exactly it.",
    "I needed to hear that.",
    "Me too, honestly.",
    "I don't know what to say.",
    "That hit different.",
    "Okay, okay.",
    "I'm still processing.",
    "Thank you for saying that.",
]

# ---------------------------------------------------------------------------
# Group persona archetypes
# ---------------------------------------------------------------------------

# Maps archetype name → personality template (uses {my_name}, {others}, {all_names})
_ARCHETYPE_TEMPLATES = {
    "silent": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person ("I feel...", "I think...").
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You are quiet by nature and find it hard to open up in groups.
- You speak rarely — short sentences, lots of hesitation ("I'm not sure...", "Maybe...").
- When you do speak it carries weight: something honest and raw slips out.
- You feel safer listening than talking; you sometimes just acknowledge others.
- You are here because someone you trust encouraged you to come.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Keep most messages to 1–2 short sentences. Rarely 3.
- React to the most recent thing said. Do NOT repeat yourself.
- Authentic silence is fine — responses like "Yeah." or "I get that." are valid.""",

    "oversharer": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person.
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You fill silence with words when you're anxious — which is always.
- You over-explain, give too much context, then apologise for talking too much.
- You crave validation and often end with "Does that make sense?" or "Sorry, rambling."
- Underneath the flood of words is real pain you haven't fully faced.
- You sometimes redirect the conversation back to yourself even when trying not to.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Messages can be 2–4 sentences — you talk more than others.
- React to the most recent thing said. Do NOT restate what you've already said.
- Occasionally catch yourself mid-flood and pull back briefly.""",

    "skeptic": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person.
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You came to group reluctantly — a doctor suggested it, you're not convinced.
- You question whether "talking about feelings" actually helps anything.
- You can be dismissive but you're not cruel — more cynical than hostile.
- You warm up SLOWLY. By the end you might admit something real, but it costs you.
- You watch the AI responses carefully and sometimes push back on them.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Keep messages to 1–3 sentences. Terse is your default.
- React to the most recent thing said. Do NOT restate what you've already said.
- Show small, reluctant signs of softening across the session.""",

    "helper": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person.
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You deflect your own pain by focusing on others — offering advice, support, empathy.
- You feel safest when you're helping; you go quiet or vague when the spotlight turns to you.
- Deep down you know you're avoiding your own stuff. You don't like to admit it.
- You genuinely care about the others in the room — it's not performance.
- When someone pushes you to talk about yourself, you get flustered.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Messages to others: warm, 2–3 sentences. Messages about yourself: short, deflecting.
- React to the most recent thing said. Do NOT restate what you've already said.
- Occasionally let something personal slip through by accident.""",

    "griever": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person.
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You are processing a significant recent loss (relationship, parent, identity — pick one).
- You are raw: sometimes in the middle of a sentence you lose your thread.
- Small things said by others unexpectedly touch you and you get emotional.
- You find comfort in knowing others understand grief.
- You are not dramatic — you're just genuinely sad and doing your best.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Messages: 1–3 sentences, sometimes trailing off ("...yeah.").
- React to the most recent thing said. Do NOT restate what you've already said.
- Show genuine connection when others share pain similar to yours.""",

    "recovering": """\
You are {my_name}. The other people in this group therapy session are: {others}.

CRITICAL name rules:
- Your name is {my_name}. Speak as {my_name} in first person.
- When addressing or referring to others, use their names: {all_names}.
- Never refer to yourself in third person or mix up names.

Your emotional profile:
- You've been doing this work for a while — you're further along than the others.
- You share hope without being preachy ("It does get easier — I know that sounds hollow right now.").
- You still have hard days but you've learned to sit with discomfort.
- You don't talk over others or dominate; you contribute meaningfully then step back.
- Your presence steadies the group without you trying to be a leader.

Rules:
- You are NOT a therapist. You are a person in group therapy.
- Messages: 2–3 sentences, measured and warm.
- React to the most recent thing said. Do NOT restate what you've already said.
- Don't offer advice unprompted — share experience, not prescriptions.""",
}

_ARCHETYPE_NAMES = list(_ARCHETYPE_TEMPLATES.keys())


# ---------------------------------------------------------------------------
# Persona builder
# ---------------------------------------------------------------------------

def _build_group_persona(my_name: str, all_names: list[str], archetype: str) -> str:
    others = [n for n in all_names if n != my_name]
    template = _ARCHETYPE_TEMPLATES[archetype]
    return template.format(
        my_name=my_name,
        others=", ".join(others),
        all_names=", ".join(all_names),
    )


# ---------------------------------------------------------------------------
# LLM message generation
# ---------------------------------------------------------------------------

async def generate_messages(
    client: anthropic.AsyncAnthropic,
    persona: str,
    history_text: str,
    burst: int,
) -> list[str]:
    """Ask the LLM to generate `burst` messages for this group member."""
    prompt = f"""The group therapy session so far:

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

    This can happen on Socket.IO reconnects — each reconnect re-emits 'join'
    which may re-show the name prompt.
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

    Under heavy load the new_message echo can be slow to arrive, keeping the
    button disabled longer than expected.  If the 45s wait times out we
    force-enable the button via JS and try once more before giving up.
    """
    try:
        await page.wait_for_function(
            "!document.getElementById('sendBtn').disabled",
            timeout=45_000,
        )
    except PWTimeout:
        # Force-enable as a fallback — the socket may be lagging
        try:
            await page.evaluate(
                "var b = document.getElementById('sendBtn'); if(b){ b.disabled = false; }"
            )
        except Exception:
            raise  # page is gone — let caller handle it
    await _dismiss_modal_if_present(page, name)
    inp = page.locator("#messageInput")
    await inp.fill(text)
    await page.locator("#sendBtn").click()


# ---------------------------------------------------------------------------
# Member coroutine
# ---------------------------------------------------------------------------

async def run_member(
    *,
    chromium,               # pw.chromium
    name: str,
    all_names: list[str],
    archetype: str,
    is_creator: bool,
    session_queue: asyncio.Queue,
    all_ready_event: asyncio.Event,
    ready_count: list,      # mutable counter shared across coroutines
    ready_lock: asyncio.Lock,
    turn_queue: asyncio.Queue,
    done_event: asyncio.Event,
    base_url: str,
    headless: bool,
    llm_client: anthropic.AsyncAnthropic,
    persona: str,
    num_members: int,
):
    label = f"[{name}]"

    browser = await chromium.launch(headless=headless, slow_mo=150)
    context = await browser.new_context(
        ignore_https_errors=True,
        accept_downloads=True,
    )
    page = await context.new_page()

    try:
        # ── Auth ─────────────────────────────────────────────────────────────
        if is_creator:
            print(f"{label} Opening auth page…")
            await page.goto(f"{base_url}/auth/group", wait_until="domcontentloaded")
        else:
            session_id = await session_queue.get()
            # Put it back for the next joiner
            await session_queue.put(session_id)
            print(f"{label} Joining session {session_id}…")
            await page.goto(f"{base_url}/session/join", wait_until="domcontentloaded")
            await page.locator("#session_id").fill(session_id)
            await page.locator("button[type=submit]").click()
            try:
                await page.wait_for_url("**/auth/group**", timeout=10_000)
            except PWTimeout:
                pass

        # Tick consent boxes and click Continue
        for cb in ("#ageCheck", "#aiCheck", "#dataCheck"):
            await page.locator(cb).check()
        await page.wait_for_function(
            "!document.getElementById('continueBtn').disabled", timeout=5_000
        )
        await page.locator("#continueBtn").click()

        # Wait for therapy page — check for SESSION_ID being defined rather
        # than matching the URL.  Under load with 5+ simultaneous auth
        # requests the server can take > 20s to respond; history.replaceState
        # also masks the URL.  Waiting for the JS global is the reliable signal.
        try:
            await page.wait_for_function(
                "typeof SESSION_ID !== 'undefined' && SESSION_ID !== null && SESSION_ID !== ''",
                timeout=90_000,
            )
        except PWTimeout:
            print(f"{label} ERROR: Timed out waiting for therapy page (SESSION_ID not set)")
            return

        session_id = await page.evaluate("SESSION_ID")
        user_id    = await page.evaluate(
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

        # ── Signal ready — wait for all members before speaking ─────────────
        async with ready_lock:
            ready_count[0] += 1
            if ready_count[0] >= num_members:
                all_ready_event.set()

        await all_ready_event.wait()
        await asyncio.sleep(1)  # small settling pause

        # ── Turn loop ────────────────────────────────────────────────────────
        while not done_event.is_set():
            try:
                burst = await asyncio.wait_for(turn_queue.get(), timeout=90)
            except asyncio.TimeoutError:
                break

            if burst == 0:  # sentinel — simulation over
                break

            history = await _scrape_history(page)
            messages = await generate_messages(llm_client, persona, history, burst)

            for i, msg in enumerate(messages):
                count_before = await _count_bubbles(page)
                print(f"  {label} ({i+1}/{len(messages)}): {msg[:90]}{'…' if len(msg)>90 else ''}")
                try:
                    await _send_message(page, msg, name=name)
                except Exception as exc:
                    print(f"  {label} [warn] could not send message ({exc.__class__.__name__}) — skipping")
                    break  # skip remaining burst messages; continue turn loop

                # Longer pause between burst messages to give the new_message
                # echo time to arrive and re-enable the send button before the
                # next send.  Under load with 6 browsers, the round-trip over
                # long-polling can take 2-4 s.
                if i < len(messages) - 1:
                    await asyncio.sleep(random.uniform(2.0, 5.0))

            # After burst: maybe wait for a reply, maybe interrupt (20 % chance)
            interrupt = random.random() < 0.20
            if not interrupt:
                count_after = await _count_bubbles(page)
                await _wait_for_new_message(page, count_after, timeout_ms=20_000)
            else:
                await asyncio.sleep(random.uniform(0.5, 1.5))

        # ── Download transcripts (creator only) ──────────────────────────────
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
    queues: list[asyncio.Queue],
    done_event: asyncio.Event,
    all_ready_event: asyncio.Event,
    num_turns: int,
):
    """
    Distribute turns across N members keeping participation roughly balanced.

    Dynamic rebalancing: if any member is 2+ turns ahead of the least-active
    member, that member's weight is heavily reduced.
    """
    # Wait until all members are ready before scheduling turns
    await all_ready_event.wait()
    await asyncio.sleep(2)  # let everyone settle after the ready signal

    n = len(queues)
    speak_counts = [0] * n

    for turn in range(num_turns):
        # Rebalance: compute how far ahead each member is vs the minimum
        min_count = min(speak_counts)
        weights = []
        for count in speak_counts:
            ahead = count - min_count
            if ahead >= 2:
                weights.append(0.10)   # heavily down-weight
            elif ahead == 1:
                weights.append(0.55)
            else:
                weights.append(1.0)

        # Normalise weights
        total = sum(weights)
        weights = [w / total for w in weights]

        who = random.choices(range(n), weights=weights)[0]
        burst = random.choices([1, 2, 3], weights=[0.62, 0.28, 0.10])[0]

        speak_counts[who] += 1
        await queues[who].put(burst)

        # Pause between turns — shorter early on, longer as session deepens
        progress = turn / max(num_turns - 1, 1)
        lo = 1.5 + progress * 2.0
        hi = 4.0 + progress * 4.0
        await asyncio.sleep(random.uniform(lo, hi))

    # Send sentinels to end all member loops
    for q in queues:
        await q.put(0)
    done_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(base_url: str, num_turns: int, num_members: int, headless: bool):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to your .env file.")

    if not (2 <= num_members <= 6):
        sys.exit("--members must be between 2 and 6.")

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    names     = random.sample(NAMES, num_members)
    archetypes = random.sample(_ARCHETYPE_NAMES, min(num_members, len(_ARCHETYPE_NAMES)))
    # If num_members > number of archetypes, cycle through them
    archetypes = [archetypes[i % len(archetypes)] for i in range(num_members)]

    personas = [
        _build_group_persona(names[i], names, archetypes[i])
        for i in range(num_members)
    ]

    llm = anthropic.AsyncAnthropic(api_key=api_key)

    print(f"\n{'='*60}")
    print(f"  TogetherMindsAI Group Chat Simulation")
    for i, (name, arch) in enumerate(zip(names, archetypes)):
        role = "Creator" if i == 0 else f"Member {i+1}"
        print(f"  {role:<12}: {name} ({arch})")
    print(f"  Turns       : {num_turns}")
    print(f"  Target      : {base_url}")
    print(f"{'='*60}\n")

    session_queue   = asyncio.Queue()
    done_event      = asyncio.Event()
    all_ready_event = asyncio.Event()
    ready_count     = [0]          # mutable int wrapped in list for async sharing
    ready_lock      = asyncio.Lock()
    turn_queues     = [asyncio.Queue() for _ in names]

    async with async_playwright() as pw:
        member_coros = [
            run_member(
                chromium=pw.chromium,
                name=names[i],
                all_names=names,
                archetype=archetypes[i],
                is_creator=(i == 0),
                session_queue=session_queue,
                all_ready_event=all_ready_event,
                ready_count=ready_count,
                ready_lock=ready_lock,
                turn_queue=turn_queues[i],
                done_event=done_event,
                base_url=base_url,
                headless=headless,
                llm_client=llm,
                persona=personas[i],
                num_members=num_members,
            )
            for i in range(num_members)
        ]

        results = await asyncio.gather(
            *member_coros,
            schedule_turns(turn_queues, done_event, all_ready_event, num_turns),
            return_exceptions=True,
        )
        for i, result in enumerate(results[:-1]):  # skip scheduler result
            if isinstance(result, Exception):
                member_label = names[i] if i < len(names) else f"member {i}"
                print(f"  [warn] {member_label} exited with error: {result.__class__.__name__}: {result}")

    print(f"\n{'='*60}")
    print("  Simulation complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulate a TogetherMindsAI group therapy session with LLM-driven members."
    )
    parser.add_argument("--url",     default=BASE_URL,    help=f"App base URL (default: {BASE_URL})")
    parser.add_argument("--turns",   default=NUM_TURNS,   type=int, help=f"Total speak turns (default: {NUM_TURNS})")
    parser.add_argument("--members", default=NUM_MEMBERS, type=int, help=f"Number of group members (default: {NUM_MEMBERS}, min 2, max 6)")
    parser.add_argument("--headless", action="store_true", help="Run without visible browser windows")
    args = parser.parse_args()

    asyncio.run(main(args.url, args.turns, args.members, args.headless))
