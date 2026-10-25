"""
ai_therapist.py
---------------
Crisis / escalation detection + the shared Anthropic client.

The AI-led response engine (text generation, emotion classification, opening
messages, etc.) was removed when the app became therapist-led only. What remains
is the safety layer reused by the therapist co-pilot:
  - keyword crisis / escalation detection (a hard rule-based pre-filter)
  - CRISIS_RESPONSE (the resources message shown to a client in crisis)
  - the lazily-created Anthropic client (also used by copilot.py)
"""

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crisis / escalation keywords — rule-based, always evaluated first
# ---------------------------------------------------------------------------

CRISIS_KEYWORDS = {
    # Direct phrases (English-original)
    "suicide", "suicidal", "kill myself", "end my life", "want to die",
    "don't want to live", "no reason to live", "self-harm", "self harm",
    "cutting myself", "hurt myself", "hurting myself", "harm myself",
    "overdose", "take my own life", "end it all", "better off dead",
    "not worth living", "rather be dead", "wish i was dead",
    # Translated-idiom phrases — defence in depth for multilingual input.
    # Even with Sonnet-based translation in /api/translate-check, machine
    # translation can flatten clinical weight from phrases unmistakable in
    # their source language. Examples: ES "no veo salida" → "I don't see a
    # way out"; JA "消えたい" → "I want to disappear"; FR "j'en ai marre de la
    # vie" → "I'm fed up with life". Including these patterns here also
    # strengthens English-original crisis detection.
    "no way out", "no escape", "can't take it anymore", "cannot take it anymore",
    "can't go on", "cannot go on", "no point in living", "no point anymore",
    "tired of living", "fed up with life", "want to disappear", "want to vanish",
    "no future", "no hope", "lost all hope", "end the pain", "stop the pain",
    "don't want to exist", "do not want to exist", "give up on life",
    "had enough of life",
}

ESCALATION_KEYWORDS = {
    "therapist", "psychiatrist", "psychologist", "counselor", "counsellor",
    "professional help", "need real help", "can't cope", "cannot cope",
    "medication", "diagnosis", "diagnosed", "ptsd", "trauma",
    "abuse", "abused", "domestic violence", "assault",
    "addiction", "substance", "alcohol", "drugs",
    "eating disorder", "anorexia", "bulimia",
}

CRISIS_RESPONSE = (
    "What you've just shared is something I want to take seriously, and I'm glad you said it. "
    "Whatever is driving these thoughts, the pain behind them is real — and you deserve real support right now.\n\n"
    "This is beyond what I'm able to hold with you safely on my own. "
    "Please reach out to someone who can be fully present with you:\n\n"
    "• Call or text 988 — Suicide & Crisis Lifeline (US, 24/7)\n"
    "• Text HOME to 741741 — Crisis Text Line (US)\n"
    "• 116 123 — Samaritans (UK & Ireland, free, 24/7)\n"
    "• 13 11 14 — Lifeline (Australia, 24/7)\n"
    "• findahelpline.com — find a helpline in your country\n"
    "• befrienders.org — worldwide emotional support\n\n"
    "If you are in immediate danger, please call emergency services now — "
    "911 (US), 999 (UK), 000 (Australia), or 112 (EU).\n\n"
    "I'm still here. If you want to keep talking while you decide what to do next, I'm with you."
)


# ---------------------------------------------------------------------------
# Anthropic client — lazy-loaded, cached for the process lifetime.
# Shared with copilot.py.
# ---------------------------------------------------------------------------

_claude_client = None


def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    return _claude_client


def claude_text(response) -> str:
    """The reply text of a Messages response: the first TEXT block, stripped.

    Never `response.content[0].text`. Sonnet 5.5 and Haiku 5.5 think by default, so
    a thinking block can come first and has no `.text`; indexing blindly turned that
    into an exception the callers swallow, i.e. a silently empty co-pilot or summary.
    Returns "" when there is no text."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) in ("thinking", "redacted_thinking"):
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text.strip()
    return ""


def _thinking_off(model: str) -> dict:
    """Request settings that keep a SHORT call from spending its token budget on
    thinking. Sonnet 5.5 rejects {"type": "disabled"} (400) and uses "between_tools"
    to turn thinking off; Haiku 5.5 accepts "disabled". Other models: leave alone."""
    name = (model or "").lower()
    if "haiku-5" in name:
        return {"thinking": {"type": "disabled"}}
    if "sonnet-5-5" in name:
        return {"thinking": {"type": "between_tools"}}
    return {}


def claude_create(client, **kwargs):
    """`client.messages.create` for a short answer, with thinking turned off.

    If the API refuses that setting (a 400), retry ONCE without it and log it, so a
    settings mismatch can never take the co-pilot or the summary down. Anything else
    is raised for the caller to handle as before."""
    extra = _thinking_off(kwargs.get("model"))
    try:
        return client.messages.create(**kwargs, **extra)
    except Exception as exc:
        if extra and exc.__class__.__name__ == "BadRequestError":
            import logging
            logging.getLogger(__name__).warning(
                "Claude refused the thinking-off setting for %s (%s); retrying without it.",
                kwargs.get("model"), exc)
            return client.messages.create(**kwargs)
        raise


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_crisis(text: str) -> bool:
    """Return True if the text contains any crisis-level language."""
    lowered = text.lower()
    return any(kw in lowered for kw in CRISIS_KEYWORDS)


def detect_escalation(text: str) -> bool:
    """Return True if the text signals a need for professional human support."""
    lowered = text.lower()
    return any(kw in lowered for kw in ESCALATION_KEYWORDS)
