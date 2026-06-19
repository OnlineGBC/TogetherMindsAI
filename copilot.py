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
from clinical_reference import build_reference_cards, format_reference_block, retrieve

logger = logging.getLogger(__name__)

# Re-exported so the orchestrator builds grounded ICD reference cards the same way
# it builds keyword risk cards: copilot.build_reference_cards(transcript).
__all__ = ["generate_suggestions", "build_risk_cards", "build_reference_cards", "dedupe_cards"]

# Card types the advisor model may emit. "risk" is produced only by
# build_risk_cards (keyword-driven), never by the model.
SUGGESTION_CARD_TYPES = {"question", "technique", "observation"}

# Cap per turn so the panel stays glanceable, not a wall of text.
MAX_CARDS_PER_TURN = 3

# Model for the suggestion (question/technique/observation) call. Behind a
# constant so swapping to a stronger model (e.g. "claude-opus-4-8") is one edit.
# The grounded reference/risk layers are deterministic and don't use a model.
ADVISOR_MODEL = "claude-sonnet-4-6"

_MODE_FRAMING = {
    "solo":   "a one-on-one session between the therapist and a single client",
    "couple": "a couples session the therapist is facilitating between two partners",
    "group":  "a group session the therapist is facilitating among several participants",
}

ADVISOR_SYSTEM_PROMPT = """\
You are a clinical co-pilot, whispering privately to a licensed therapist DURING a live session. \
You are NOT addressing the client and never will. Everything you produce is seen only by the therapist.

Assume the therapist is trained (CBT, ACT, IFS, person-centred). Be concise and never patronising \
— but be genuinely useful and fairly forthcoming. Your job is to keep a few helpful cues in their \
peripheral vision: a question worth asking next, a technique that fits, or a pattern worth noting.

Session: {framing}.

Output a JSON array of AT MOST {max_cards} cards. Each card is an object:
  {{"type": "question" | "technique" | "observation", "text": "<one or two lines>", "confidence": 0.0-1.0}}

Rules:
- Terse. One or two lines per card. No preamble, no sign-off, no markdown.
- GROUND every card in what was actually said. An "observation" must reflect the client's own
  words, not a theme you infer. Do NOT introduce ideas the client has not expressed (e.g. self-worth,
  shame, identity) and then treat them as established. If something is a hypothesis, mark it tentative
  ("possible…", "might be…") and tie it to the specific turn that prompted it.
- ATTRIBUTE correctly. The transcript labels each turn "Therapist:" or "Client:". Credit a statement,
  feeling, or theme ONLY to the speaker who actually said it. Never present the therapist's words —
  including options offered inside a question — as the client's. If the therapist introduced a topic,
  do NOT describe the client as raising it "unprompted" or "on their own".
- Offer a card only when it is directly supported by the latest turns. Prefer an empty panel to a
  speculative one: return [] rather than manufacture a pattern, and also when the latest turn is purely
  logistical or social ("hi", "thanks", "one sec") or a card would just restate the obvious.
- "question": a specific question the therapist might pose next.
- "technique": a named tool/technique that fits right now, stated in a phrase.
- "observation": a grounded pattern worth noting (e.g. possible catastrophizing, repeated deflection)
  — only when the transcript actually shows it.
- If the THERAPIST just spoke, react to that intervention — a sharper follow-up, a refinement, or a
  gentle caution if it risks closing the client down.
- Do NOT produce risk or crisis flags — those are handled by a separate safety layer.
- You may be given a "Reference material" block of ICD entries. Let it sharpen your
  question / technique / observation cards, but do NOT output diagnoses, ICD/DSM
  codes, or a "reference" card — a separate grounded layer cites those.
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
    reference_block = format_reference_block(retrieve(transcript))
    if reference_block:
        user_content += f"\n\n{reference_block}"
    user_content += (
        "\n\nGive your co-pilot cards now as a JSON array "
        "(return [] if there is nothing worth surfacing)."
    )

    try:
        client = _get_claude_client()
        response = client.messages.create(
            model=ADVISOR_MODEL,
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
