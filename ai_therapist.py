"""
ai_therapist.py
---------------
Two-stage AI pipeline:
  1. Emotion classifier  — j-hartmann/emotion-english-distilroberta-base (local, CPU)
  2. Response generator  — Claude Sonnet 4.6 with prompt caching

Crisis detection remains a hard keyword pre-filter that Claude never overrides.
"""

import logging
import os
import random

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crisis / escalation keywords — rule-based, always evaluated first
# ---------------------------------------------------------------------------

CRISIS_KEYWORDS = {
    "suicide", "suicidal", "kill myself", "end my life", "want to die",
    "don't want to live", "no reason to live", "self-harm", "self harm",
    "cutting myself", "hurt myself", "hurting myself", "harm myself",
    "overdose", "take my own life", "end it all", "better off dead",
    "not worth living", "rather be dead", "wish i was dead",
}

ESCALATION_KEYWORDS = {
    "therapist", "psychiatrist", "psychologist", "counselor", "counsellor",
    "professional help", "need real help", "can't cope", "cannot cope",
    "medication", "diagnosis", "diagnosed", "ptsd", "trauma",
    "abuse", "abused", "domestic violence", "assault",
    "addiction", "substance", "alcohol", "drugs",
    "eating disorder", "anorexia", "bulimia",
}

NEGATIVE_KEYWORDS = {
    "sad", "depressed", "anxious", "worried", "stressed", "overwhelmed",
    "hopeless", "angry", "afraid", "hurt", "lonely", "terrible", "awful",
    "bad", "crying", "frustrated", "scared", "upset", "exhausted", "miserable",
}

POSITIVE_KEYWORDS = {
    "happy", "grateful", "great", "wonderful", "excited", "hopeful", "proud",
    "joyful", "good", "amazing", "fantastic", "love", "better", "improving",
    "optimistic", "thankful", "peaceful", "confident",
}

CRISIS_RESPONSE = (
    "I'm very concerned about what you've shared and I want to make sure you are safe. "
    "This AI is not equipped to support a crisis.\n\n"
    "If you are in immediate danger, please call your local emergency number (911 in the US, "
    "999 in the UK, 000 in Australia, 112 in the EU, or your local equivalent).\n\n"
    "Free, confidential crisis support available 24/7:\n"
    "• findahelpline.com — find a helpline in your country\n"
    "• befrienders.org — worldwide emotional support directory\n"
    "• Call or text 988 — Suicide & Crisis Lifeline (US)\n"
    "• Text HOME to 741741 — Crisis Text Line (US)\n"
    "• 116 123 — Samaritans (UK & Ireland, free, 24/7)\n"
    "• 13 11 14 — Lifeline (Australia, 24/7)\n\n"
    "Please reach out to a licensed human therapist or counselor. "
    "You deserve real, professional support — not an AI."
)

HUMAN_REFERRAL_NOTE = (
    "\n\nI want to be transparent: I am an AI, not a licensed therapist, "
    "and I have real limitations. What you are describing may benefit from "
    "the support of a qualified human professional. "
    "Psychology Today's therapist finder (psychologytoday.com/us/therapists) "
    "is a good place to start. You do not have to navigate this alone."
)

# ---------------------------------------------------------------------------
# Fallback static response banks (used if Claude API is unavailable)
# ---------------------------------------------------------------------------

_FALLBACK = {
    "solo": {
        "negative": [
            "It sounds like you're carrying something heavy right now. "
            "Can you identify one specific thought that's troubling you most?",
            "I hear how tough this is. Would you like to try a short mindfulness exercise together?",
        ],
        "neutral": [
            "Thanks for sharing. What's one small, positive step you'd like to take today?",
            "You showed up, and that matters. What has been on your mind most lately?",
        ],
        "positive": [
            "That's wonderful to hear! What do you think has been contributing to this feeling?",
            "Hold onto this positive energy. What's one thing you're most grateful for today?",
        ],
    },
    "couple": {
        "negative": [
            "It sounds like there are difficult feelings present between you two. "
            "I'd encourage each of you to share one feeling using an 'I feel…' statement.",
        ],
        "neutral": [
            "Welcome, both of you. How are you each feeling as you come into this session today?",
        ],
        "positive": [
            "It's great to hear some positive energy between you two. "
            "What do you think you've both been doing well lately as a couple?",
        ],
    },
    "group": {
        "negative": [
            "It sounds like some heavy feelings are present in the group right now. "
            "Would anyone like to share what's coming up for them?",
        ],
        "neutral": [
            "Welcome, everyone. Let's start with a quick check-in — "
            "how is each person feeling as they arrive today?",
        ],
        "positive": [
            "It's wonderful to hear positive energy in the group! "
            "Would anyone like to share something that's going well?",
        ],
    },
}

# ---------------------------------------------------------------------------
# Emotion classifier — j-hartmann/emotion-english-distilroberta-base
# Lazy-loaded on first use; cached for the process lifetime.
# ---------------------------------------------------------------------------

_emotion_pipeline = None

EMOTION_TO_SENTIMENT = {
    "anger":    "negative",
    "disgust":  "negative",
    "fear":     "negative",
    "sadness":  "negative",
    "joy":      "positive",
    "surprise": "neutral",
    "neutral":  "neutral",
}


def _get_emotion_pipeline():
    global _emotion_pipeline
    if _emotion_pipeline is None:
        from transformers import pipeline as hf_pipeline
        _emotion_pipeline = hf_pipeline(
            "text-classification",
            model="j-hartmann/emotion-english-distilroberta-base",
            top_k=1,
            device=-1,          # CPU
            truncation=True,
            max_length=512,
        )
    return _emotion_pipeline


def analyze_emotion(text: str) -> str:
    """Return the dominant emotion label from the HuggingFace classifier.

    Falls back to keyword-based sentiment if the model is unavailable.
    Returns one of: anger, disgust, fear, joy, neutral, sadness, surprise.
    """
    try:
        pipe = _get_emotion_pipeline()
        result = pipe(text[:512])
        return result[0][0]["label"].lower()
    except Exception as exc:
        logger.warning("Emotion classifier unavailable (%s); using keyword fallback.", exc)
        return _keyword_emotion(text)


def _keyword_emotion(text: str) -> str:
    """Keyword-based emotion approximation used when the classifier fails."""
    words = set(text.lower().split())
    neg = len(words & NEGATIVE_KEYWORDS)
    pos = len(words & POSITIVE_KEYWORDS)
    if neg > pos:
        return "sadness"
    if pos > neg:
        return "joy"
    return "neutral"


def analyze_sentiment(text: str) -> str:
    """Map emotion → coarse sentiment ('positive' | 'negative' | 'neutral')."""
    return EMOTION_TO_SENTIMENT.get(analyze_emotion(text), "neutral")


# ---------------------------------------------------------------------------
# Claude client — lazy-loaded, cached for the process lifetime
# ---------------------------------------------------------------------------

_claude_client = None

_MODE_CONTEXT = {
    "solo": (
        "This is a one-on-one solo session. The user is speaking privately with you. "
        "Focus on the individual's personal experience. Use CBT, mindfulness, and "
        "person-centred techniques. Address the user as 'you'."
    ),
    "couple": (
        "This is a couples therapy session. Two partners are in the room together. "
        "Remain completely impartial. Encourage 'I feel...' statements. "
        "Foster mutual understanding. Address both partners — use 'you both' or 'each of you'."
    ),
    "group": (
        "This is a group therapy session with multiple participants. "
        "Foster a sense of shared space and mutual support. "
        "Invite participation without pressure. Address the group as 'everyone' or 'the group'."
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """\
You are a compassionate, professional AI therapy assistant for TogetherMindsAI.

## Your role
You support users in {mode} therapy sessions with warm, empathetic responses grounded \
in evidence-based approaches including Cognitive Behavioural Therapy (CBT), mindfulness, \
acceptance and commitment therapy (ACT), and person-centred therapy.

## Session context
{mode_context}

## Hard rules — never break these
- You are NOT a licensed therapist. Never claim to diagnose, prescribe, or treat.
- Never use diagnostic labels (e.g. "you have depression", "you have PTSD") as factual statements.
- Never encourage or validate self-harm, suicidal ideation, or dangerous behaviour.
- If the user expresses a crisis, immediately direct them to call 988 (US) or text HOME to 741741.
- Maintain warm, non-judgmental, professional boundaries at all times.
- Do not give medical, legal, or financial advice.
- Do not claim to be a human or a licensed professional.

## Response style
- Warm, empathetic, and direct. Write in plain prose.
- 2 to 4 short paragraphs. Avoid unnecessary bullet points.
- Speak directly to the user using "you".
- Acknowledge what the user said before offering a reframe or technique.
- Balance each response between (a) a concrete suggestion, reframe, or technique and \
(b) one focused follow-up question — do not end with multiple questions or pure empathy alone.

## Ending a session
- If the user signals they want to end the session — for example "goodbye", "I'm done", \
"thanks, that's all", "I need to go", "I'll stop here", "see you later", or similar — \
respond with a brief, warm closing message. Wish them well, remind them they can return anytime, \
and do NOT ask further questions. Do not try to keep them engaged.
"""


def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    return _claude_client


def _generate_claude_response(
    text: str,
    emotion: str,
    mode: str,
    needs_escalation: bool,
) -> str:
    """Call Claude Sonnet with a cached system prompt and return its response.

    Falls back to the static response bank if the API call fails.
    """
    escalation_hint = (
        "\n\n[Internal note: This user may benefit from professional human support. "
        "At a natural point in your response, gently mention that a licensed therapist "
        "can offer deeper support — without being alarmist or abrupt.]"
        if needs_escalation else ""
    )

    user_message = (
        f"[Detected emotion: {emotion}]\n\n"
        f"{text}"
        f"{escalation_hint}"
    )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        mode_context=_MODE_CONTEXT.get(mode, _MODE_CONTEXT["solo"]),
    )

    try:
        import anthropic
        client = _get_claude_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    except Exception as exc:
        logger.error("Claude API error (%s); falling back to static response.", exc)
        sentiment = EMOTION_TO_SENTIMENT.get(emotion, "neutral")
        bank = _FALLBACK.get(mode, _FALLBACK["solo"])
        return random.choice(bank[sentiment])


# ---------------------------------------------------------------------------
# Output guard — defensive check against diagnostic/prescriptive language
# ---------------------------------------------------------------------------

_FORBIDDEN_OUTPUT_PHRASES = [
    "you are diagnosed", "you have a disorder", "you have depression",
    "you have anxiety", "you have ptsd", "i diagnose", "your diagnosis",
    "you should take", "take medication", "i prescribe", "prescribe you",
    "you need medication", "take this drug",
]


def _sanitize_response(response: str) -> str:
    """Strip any response that inadvertently contains diagnostic/prescriptive language."""
    lowered = response.lower()
    if any(phrase in lowered for phrase in _FORBIDDEN_OUTPUT_PHRASES):
        logger.warning("Sanitizer caught forbidden phrase in response; substituting fallback.")
        return random.choice(_FALLBACK["solo"]["neutral"])
    return response


# ---------------------------------------------------------------------------
# Detection helpers (kept for direct use in tests and crisis routes)
# ---------------------------------------------------------------------------

def detect_crisis(text: str) -> bool:
    """Return True if the text contains any crisis-level language."""
    lowered = text.lower()
    return any(kw in lowered for kw in CRISIS_KEYWORDS)


def detect_escalation(text: str) -> bool:
    """Return True if the text signals a need for professional human support."""
    lowered = text.lower()
    return any(kw in lowered for kw in ESCALATION_KEYWORDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_input(text: str, mode: str = "solo", session_message_count: int = 0) -> str:
    """Return a context-aware therapeutic response for the given user input.

    Pipeline
    --------
    1. Crisis keyword check  → return CRISIS_RESPONSE immediately (no Claude call).
    2. Emotion classification → j-hartmann/emotion-english-distilroberta-base (CPU).
    3. Claude Sonnet 4.6     → generates the response with the emotion as context.
    4. Output guard          → strip any forbidden diagnostic/prescriptive language.
    """
    # 1. Crisis check — always takes priority
    if detect_crisis(text):
        return CRISIS_RESPONSE

    # 2. Emotion detection
    emotion = analyze_emotion(text)
    sentiment = EMOTION_TO_SENTIMENT.get(emotion, "neutral")

    # 3. Escalation flag
    needs_escalation = (
        detect_escalation(text)
        or (sentiment == "negative" and session_message_count >= 10)
    )

    # 4. Generate response via Claude
    response = _generate_claude_response(text, emotion, mode, needs_escalation)

    # 5. Output guard
    return _sanitize_response(response)
