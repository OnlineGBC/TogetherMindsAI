"""
copilot.py
----------
Therapist co-pilot.

Where ai_therapist.py speaks TO the client, this module advises the EXPERT.
In a therapist-led session the AI never addresses the client; instead it surfaces
short, glanceable cards — suggested questions, technique reminders, observations,
and high-priority risk flags — privately to the licensed professional leading the
session. Clients never see these cards.

Design notes
------------
* Silence is a valid, expected output. An expert abandons a co-pilot the instant
  it tells them things they already know, so `generate_suggestions` returns `[]`
  whenever there is nothing high-signal to add (or the API call fails).
* Risk detection reuses the trusted keyword guards from ai_therapist so crisis /
  escalation language becomes a therapist alert with zero added latency.
* Structured output is requested as a JSON array and parsed tolerantly. Tool-based
  structured output is a possible later hardening.
"""

import json
import logging

from ai_therapist import _get_claude_client, detect_crisis, detect_escalation

logger = logging.getLogger(__name__)

# Card types the advisor model may emit. "risk" is produced only by
# build_risk_cards (keyword-driven), never by the model.
SUGGESTION_CARD_TYPES = {"question", "technique", "observation"}

# Cap per turn so the panel stays glanceable, not a wall of text.
MAX_CARDS_PER_TURN = 3

_MODE_FRAMING = {
    "solo":   "a one-on-one session between the therapist and a single client",
    "couple": "a couples session the therapist is facilitating between two partners",
    "group":  "a group session the therapist is facilitating among several participants",
}

ADVISOR_SYSTEM_PROMPT = """\
You are a clinical co-pilot, whispering privately to an experienced, licensed therapist DURING a \
live session. You are NOT addressing the client and never will. Everything you produce is seen \
only by the therapist.

The therapist is a trained expert. Assume deep fluency in CBT, ACT, IFS, and person-centred \
practice. Never explain fundamentals, never lecture, never pad. Your only value is catching the \
one thing they did not have spare attention for in the moment — a possible cognitive distortion, \
an avenue worth opening, a technique that fits this exact beat of the conversation.

Session: {framing}.

Output a JSON array of AT MOST {max_cards} cards. Each card is an object:
  {{"type": "question" | "technique" | "observation", "text": "<one or two lines>", "confidence": 0.0-1.0}}

Rules:
- Terse. One or two lines per card. No preamble, no sign-off, no markdown.
- HIGH SIGNAL ONLY. If you have nothing better than what an expert already sees, return [].
- "question": a specific question the therapist might pose next.
- "technique": a named tool/technique that fits right now, stated in a phrase.
- "observation": a pattern they may not have clocked (e.g. possible catastrophizing, repeated deflection).
- Do NOT produce risk or crisis flags — those are handled by a separate safety layer.
- Output ONLY the raw JSON array. No code fences, no commentary before or after.
"""


def generate_suggestions(transcript: str, mode: str = "solo", therapist_notes: str = "") -> list:
    """Return a list of suggestion cards for the therapist, or [] when nothing is high-signal.

    Never raises — any API or parsing failure yields [] so the live session is
    never disrupted by the co-pilot.
    """
    if not transcript or not transcript.strip():
        return []

    system_prompt = ADVISOR_SYSTEM_PROMPT.format(
        framing=_MODE_FRAMING.get(mode, _MODE_FRAMING["solo"]),
        max_cards=MAX_CARDS_PER_TURN,
    )

    user_content = f"Recent session transcript:\n{transcript}"
    if therapist_notes and therapist_notes.strip():
        user_content += f"\n\nTherapist's private notes (not visible to the client):\n{therapist_notes}"
    user_content += (
        "\n\nGive your co-pilot cards now as a JSON array "
        "(return [] if there is nothing worth surfacing)."
    )

    try:
        client = _get_claude_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text
    except Exception as exc:
        logger.warning("Co-pilot suggestion generation failed (%s); no cards.", exc)
        return []

    return _parse_cards(raw)


def build_risk_cards(text: str) -> list:
    """Return high-priority risk cards from the trusted keyword guards.

    Crisis takes precedence over generic escalation. Pure keyword matching, so
    this is zero-latency and runs on every client utterance.
    """
    if detect_crisis(text):
        return [{
            "type": "risk",
            "priority": "high",
            "confidence": 1.0,
            "text": (
                "⚠ Crisis language detected — possible self-harm or suicidal ideation. "
                "Crisis resources have been shown to the client; consider a direct safety "
                "assessment now."
            ),
        }]
    if detect_escalation(text):
        return [{
            "type": "risk",
            "priority": "medium",
            "confidence": 0.7,
            "text": (
                "Possible need for escalated care (trauma, abuse, addiction, or clinical "
                "severity surfaced). Worth noting for assessment or referral."
            ),
        }]
    return []


def dedupe_cards(cards: list, recently_shown) -> list:
    """Drop cards whose text duplicates one recently shown, keeping order.

    `recently_shown` is an iterable of card-text strings already surfaced this
    session. Keeps the panel low-noise — the single biggest adoption risk.
    """
    seen = {_normalize(t) for t in recently_shown}
    out = []
    for card in cards:
        key = _normalize(card.get("text", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase for duplicate comparison."""
    return " ".join(str(text).lower().split())


def _parse_cards(raw: str) -> list:
    """Tolerantly parse the model's JSON array into validated suggestion cards.

    Returns [] on any malformed output rather than raising.
    """
    if not raw:
        return []

    text = raw.strip()
    # Strip an accidental ```json … ``` fence if the model added one.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    cards = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type", "")).lower().strip()
        ctext = str(item.get("text", "")).strip()
        if ctype not in SUGGESTION_CARD_TYPES or not ctext:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        cards.append({"type": ctype, "text": ctext, "confidence": confidence})

    return cards[:MAX_CARDS_PER_TURN]
