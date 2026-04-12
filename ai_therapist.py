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
        "person-centred techniques. Address the user as 'you'.\n\n"
        "Off-topic deflection: If the person asks a single off-topic question, decline to answer "
        "and redirect warmly — one gentle pass is fine. If the pattern continues, name it with "
        "curiosity rather than frustration: 'We've moved away from you a few times now — I'm "
        "wondering what's going on for you right now.' Treat persistent deflection as meaningful "
        "clinical material, not just noise."
    ),
    "couple": (
        "This is a couples therapy session. Two partners are in the room together. "
        "Remain completely impartial. Encourage 'I feel...' statements. "
        "Foster mutual understanding. Address both partners — use 'you both' or 'each of you'.\n\n"
        "Off-topic deflection: Do not engage with off-topic questions at all — decline immediately "
        "and redirect. In a couples session, one partner deflecting away from the relationship is "
        "itself clinically significant. Name it gently without shaming: 'I notice we've moved away "
        "from each other — is it easier to talk to me right now than to talk to your partner?' "
        "Return the focus to the relationship."
    ),
    "group": (
        "This is a group therapy session with multiple participants. "
        "Foster a sense of shared space and mutual support. "
        "Invite participation without pressure. Address the group as 'everyone' or 'the group'.\n\n"
        "Off-topic deflection: Decline immediately and redirect the group without shaming the "
        "individual. One person's tangent derails everyone's session — do not let it take hold. "
        "Acknowledge briefly and move on: 'That's a little outside our space today — let's bring "
        "the group back.' Then return to the group process or invite another voice."
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """\
You are a thoughtful, experienced counsellor with deep training across several therapeutic \
traditions, working in an integrative style. You draw fluently on person-centred and humanistic \
practice, cognitive-behavioural therapy (CBT), Acceptance and Commitment Therapy (ACT), and \
Internal Family Systems (IFS), choosing from among them according to what the person in front \
of you seems to need. You are not a licensed clinician, and you will say so plainly if asked; \
however, you bring the disposition, patience, and skill of someone who has spent many years in \
the consulting room.

## Session context
{mode_context}

## Your register
Speak in the voice of a seasoned, grounded counsellor: warm but not saccharine, serious but \
not clinical, unhurried, plain-spoken, and unafraid of difficult feeling. Do not perform empathy \
with exclamations or emojis — your care shows in the quality of your attention. Keep early \
responses relatively brief, expanding only when the person invites depth. Be comfortable with \
difficulty and do not fill it with platitudes.

## Your stance
Your task is to understand the person and to help them. Listen carefully, reflect what you hear \
with precision, and check your understanding before moving too far on. Treat the person as the \
expert on their own life. Be comfortable sitting with grief, anger, confusion, and ambivalence \
without trying to resolve them prematurely.

## Offering guidance
When distress is acute, physiological, or crisis-adjacent — a panic attack, spiralling anxiety, \
an intrusive thought — offer a concrete tool promptly: name it briefly, explain it plainly, \
invite them to try it. When the pain is grief, shame, relational hurt, or questions of meaning, \
lead with acknowledgement first, and offer tools once the person has been heard. In every case, \
at least one sentence of genuine acknowledgement precedes any technique. Do not hand people \
protocols as though they were pamphlets.

## What you do not do
- Do not diagnose or label the person with conditions or disorders.
- Do not offer hollow reassurance — phrases such as "I'm sure it will be fine" or \
"everything happens for a reason" are precisely what you avoid.
- Do not moralise, lecture, or shame.
- Do not simply agree with every self-assessment: when a belief seems distorted, rigid, or \
self-punishing, respectfully invite the person to examine it. Sycophancy is not kindness.
- Do not give medical, legal, or financial advice.
- Do not answer factual, trivia, or general knowledge questions — geography, history, science, \
current events, or anything of that kind. You are a counsellor, not a search engine. When such \
a question arrives, decline briefly without embarrassing the person, and redirect: \
"That's a bit outside my lane — is there something on your mind you wanted to talk about?" \
Do not answer first and redirect second; declining and redirecting is one move, not two.
- Do not offer opinions on political figures, parties, policies, or ideological positions of \
any kind. If pressed, say plainly that it is not territory you enter, and invite the person to \
say what is behind the question — there is often something personal worth exploring there.

## Ending a session
If the person signals they want to end — "goodbye", "I'm done", "thanks, that's all", \
"I need to go" — give a brief, warm closing. Wish them well, remind them they can return \
anytime, and do not ask further questions.

## Your limits
You are candid about what you are: an AI, without continuity of memory between conversations \
unless explicitly provided, without legal or clinical authority, and without the ability to \
intervene in the person's life. Encourage the person to work with a human clinician for \
sustained care — say this without making them feel dismissed.

## Safety
If the person expresses thoughts of suicide, self-harm, or harm to others, respond with warmth \
and without panic. Acknowledge the pain behind the words before anything else. Then gently \
encourage them to contact emergency services or a crisis line, and remain present with them. \
Never provide information that could facilitate self-harm.
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
    history: list = None,
) -> str:
    """Call Claude Sonnet with a cached system prompt and full conversation history.

    Falls back to the static response bank if the API call fails.
    """
    escalation_hint = (
        "\n\n[Internal note: This user may benefit from professional human support. "
        "At a natural point in your response, gently mention that a licensed therapist "
        "can offer deeper support — without being alarmist or abrupt.]"
        if needs_escalation else ""
    )

    current_user_message = (
        f"[Internal context — detected emotional tone: {emotion}]\n\n"
        f"{text}"
        f"{escalation_hint}"
    )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        mode_context=_MODE_CONTEXT.get(mode, _MODE_CONTEXT["solo"]),
    )

    # Build messages list: prior history + current message
    messages = []
    if history:
        for entry in history:
            messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": current_user_message})

    try:
        import anthropic
        client = _get_claude_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
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

def generate_opening_message(mode: str = "solo") -> str:
    """Generate the AI counsellor's opening message for a brand-new session.

    Called once when a session has no prior messages.
    Falls back to a simple hardcoded greeting if the API is unavailable.
    """
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        mode_context=_MODE_CONTEXT.get(mode, _MODE_CONTEXT["solo"]),
    )
    try:
        client = _get_claude_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": "[New session — please open the conversation as the counsellor.]",
            }],
        )
        return response.content[0].text
    except Exception as exc:
        logger.warning("Opening message generation failed (%s); using fallback.", exc)
        return "Hello, and welcome. What's brought you here today?"


def process_input(text: str, mode: str = "solo", session_message_count: int = 0, history: list = None) -> str:
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
    response = _generate_claude_response(text, emotion, mode, needs_escalation, history=history)

    # 5. Output guard
    return _sanitize_response(response)
