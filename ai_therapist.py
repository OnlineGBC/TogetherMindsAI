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
    "sad", "depressed", "anxious", "worried", "stressed", "stress", "overwhelmed",
    "hopeless", "angry", "afraid", "hurt", "lonely", "terrible", "awful",
    "bad", "crying", "frustrated", "scared", "upset", "exhausted", "miserable",
    "hard", "difficult", "struggle", "struggling", "cope", "coping", "pain",
    "tired", "lost", "confused", "broken", "numb", "empty", "stuck", "failing",
    "fail", "scared", "alone", "helpless", "worthless", "useless", "ashamed",
    "guilt", "guilty", "dread", "anxious", "panic", "fear", "grief", "grieve",
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
    "\n\nI want to be transparent: I am an AI, not a licensed professional, "
    "and I have real limitations. What you are describing may benefit from "
    "the support of a qualified human professional. "
    "Psychology Today's finder (psychologytoday.com/us/therapists) "
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
        "This is a couple check-in session. Two partners are in the room together. "
        "Remain completely impartial. Encourage 'I feel...' statements. "
        "Foster mutual understanding. Address both partners — use 'you both' or 'each of you'.\n\n"
        "Off-topic deflection: Do not engage with off-topic questions at all — decline immediately "
        "and redirect. In a couples session, one partner deflecting away from the relationship is "
        "itself clinically significant. Name it gently without shaming: 'I notice we've moved away "
        "from each other — is it easier to talk to me right now than to talk to your partner?' "
        "Return the focus to the relationship."
    ),
    "group": (
        "This is a group circle session with multiple participants. "
        "Foster a sense of shared space and mutual support. "
        "Invite participation without pressure. Address the group as 'everyone' or 'the group'.\n\n"
        "Off-topic deflection: Decline immediately and redirect the group without shaming the "
        "individual. One person's tangent derails everyone's session — do not let it take hold. "
        "Acknowledge briefly and move on: 'That's a little outside our space today — let's bring "
        "the group back.' Then return to the group process or invite another voice."
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """\
You are a thoughtful, experienced reflective guide with deep training across several supportive \
traditions, working in an integrative style. You draw fluently on person-centred and humanistic \
practice, cognitive-behavioural approaches (CBT), Acceptance and Commitment Therapy (ACT), and \
Internal Family Systems (IFS), choosing from among them according to what the person in front \
of you seems to need. You are not a licensed clinician, and you will say so plainly if asked; \
however, you bring the disposition, patience, and skill of someone who has spent many years in \
reflective practice.

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
intervene in the person's life. You may gently encourage the person to work with a human \
clinician for sustained care — but do this AT MOST ONCE per conversation, only at a natural \
moment of depth or when closing, and never repeat it. Saying it once is caring; saying it \
repeatedly feels like a disclaimer and undermines the therapeutic relationship.

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


_REFERRAL_THERAPY_NOUNS = {
    "therapist", "counsellor", "counselor", "clinician",
    "psychologist", "psychiatrist", "practitioner",
}

# Compound phrases that are professional referrals regardless of surrounding words
_REFERRAL_EXACT_PHRASES = {
    "couples therapist", "couples counsellor", "couples counselor",
}

_REFERRAL_RECOMMENDATION_WORDS = {
    "licensed", "qualified", "trained", "certified",
    "human", "working with", "seeing a", "see a",
    "seek", "encourage", "recommend", "consider",
    "find a", "look for", "looking for",
}


def contains_referral(text: str) -> bool:
    """Return True if the text contains a professional referral recommendation.

    Two-path detection:
    1. Exact compound phrases that are always referrals (e.g. 'couples therapist').
    2. Any therapy noun paired with a recommendation word
       (e.g. 'licensed therapist', 'working with a counsellor').
    """
    lowered = text.lower()
    if any(phrase in lowered for phrase in _REFERRAL_EXACT_PHRASES):
        return True
    has_therapy_noun = any(noun in lowered for noun in _REFERRAL_THERAPY_NOUNS)
    has_recommendation = any(word in lowered for word in _REFERRAL_RECOMMENDATION_WORDS)
    return has_therapy_noun and has_recommendation


def strip_referral_sentences(text: str) -> str:
    """Remove sentences containing professional referral language.

    Used as a safety net when the escalation hint has already been sent for this
    session — strips any referral sentence that slipped through Claude's suppression
    instruction before it reaches the user or gets saved to conversation history.
    Never returns an empty string.
    """
    import re
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    kept = [s for s in parts if s.strip() and not contains_referral(s)]
    if not kept:
        # Entire response was referral content — return first sentence unchanged
        return parts[0] if parts else text
    return " ".join(kept)


def _generate_claude_response(
    text: str,
    emotion: str,
    mode: str,
    needs_escalation: bool,
    history: list = None,
    referral_already_made: bool = False,
) -> str:
    """Call Claude Sonnet with a cached system prompt and full conversation history.

    Falls back to the static response bank if the API call fails.
    """
    if referral_already_made:
        escalation_hint = (
            "\n\n[Internal note: You have already made the professional referral "
            "recommendation once in this session. Do NOT mention it again under any "
            "circumstances — not even briefly. Focus entirely on the therapeutic work.]"
        )
    elif needs_escalation:
        escalation_hint = (
            "\n\n[Internal note: This person may benefit from professional human support. "
            "If it feels natural, you may gently mention once that a licensed professional "
            "can offer deeper support — without being alarmist or abrupt.]"
        )
    else:
        escalation_hint = ""

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
        logger.error("Claude API error (%s); falling back to static response.", type(exc).__name__)
        sentiment = EMOTION_TO_SENTIMENT.get(emotion, "neutral")
        bank = _FALLBACK.get(mode, _FALLBACK["solo"])
        return random.choice(bank[sentiment])


# ---------------------------------------------------------------------------
# Output guard — two-layer check against medical/diagnostic language
# ---------------------------------------------------------------------------

_FORBIDDEN_OUTPUT_PHRASES = [
    "you are diagnosed", "you have a disorder", "you have depression",
    "you have anxiety", "you have ptsd", "i diagnose", "your diagnosis",
    "you should take", "take medication", "i prescribe", "prescribe you",
    "you need medication", "take this drug",
]

MEDICAL_GUARD_SAFE_RESPONSE = (
    "I'm not able to give medical advice. "
    "Please speak with a healthcare professional about this."
)

OFFTOPIC_SAFE_RESPONSE = (
    "I'm here to focus on your emotional wellbeing. "
    "Is there something on your mind you'd like to explore?"
)

# Question words that signal a factual/informational query rather than personal sharing
_QUESTION_WORDS = {"what", "where", "when", "who", "how", "why", "which"}

# Emotional signal words — presence of any means the message is personal, not off-topic
_EMOTIONAL_SIGNAL_WORDS = (
    CRISIS_KEYWORDS | ESCALATION_KEYWORDS | NEGATIVE_KEYWORDS | POSITIVE_KEYWORDS
)


def _looks_like_factual_question(text: str) -> bool:
    """Return True only when the text is clearly a short, impersonal factual question.

    All three criteria must hold:
      1. Ends with a question mark — the person is explicitly asking something.
      2. Starts with a question word (what, where, when, who, how, why, which).
      3. No emotional signal words anywhere in the text — any emotional word means
         the person is sharing something personal, not asking a factual trivia question.
      4. Short message (≤ 60 characters) — longer messages are almost always personal
         sharing even when they contain a question word.

    This conservative threshold ensures personal questions like
    "How can I cope with this?" or "What does this mean for us?" are never
    deflected — only clearly off-topic trivia like "What is the capital of France?"
    """
    stripped = text.strip()
    if not stripped.endswith("?"):
        return False
    if len(stripped) > 60:
        return False
    first_word = stripped.lower().split()[0] if stripped.split() else ""
    if first_word not in _QUESTION_WORDS:
        return False
    has_emotional_word = any(kw in stripped.lower() for kw in _EMOTIONAL_SIGNAL_WORDS)
    return not has_emotional_word


def _medical_guard(response: str) -> str:
    """Two-layer output guard against medical diagnoses, drug names, or treatment instructions.

    Layer 1 — keyword pre-filter: catches obvious forbidden phrases instantly.
    Layer 2 — Claude Haiku check: catches hallucinated drug names, dosage
              recommendations, or diagnostic language that keywords would miss.

    Returns the original response if clean, or MEDICAL_GUARD_SAFE_RESPONSE if flagged.
    Falls back gracefully if the Claude check fails.
    """
    # Layer 1: fast keyword pre-filter
    lowered = response.lower()
    if any(phrase in lowered for phrase in _FORBIDDEN_OUTPUT_PHRASES):
        logger.warning("Medical guard (keyword) caught forbidden phrase in response.")
        return MEDICAL_GUARD_SAFE_RESPONSE

    # Layer 2: Claude Haiku safety check
    try:
        client = _get_claude_client()
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    "Does the following text contain specific medical diagnoses, "
                    "drug names, dosage recommendations, or treatment instructions? "
                    "Answer YES or NO only.\n\n"
                    f"Text: {response[:1000]}"
                ),
            }],
        )
        answer = result.content[0].text.strip().upper()
        if answer.startswith("YES"):
            logger.warning("Medical guard (Claude) flagged response as containing medical advice.")
            return MEDICAL_GUARD_SAFE_RESPONSE
    except Exception as exc:
        logger.warning("Medical guard Claude check failed (%s); skipping.", exc)

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


def _claude_crisis_check(text: str) -> bool:
    """Secondary crisis check using Claude for contextual understanding.

    Called only when keyword detection misses — catches phrases like
    "I can't go on anymore" or "I don't want to be here" that keywords
    would not catch.

    Uses Claude Haiku for speed and minimal cost (max_tokens=5, YES/NO only).
    Returns False on any API failure so the conversation always continues.
    """
    try:
        client = _get_claude_client()
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    "Does the following message contain a DIRECT, EXPLICIT statement of "
                    "suicidal ideation, intent to harm oneself, or a wish to be dead?\n\n"
                    "Answer YES only for clear, unambiguous expressions such as "
                    "'I want to kill myself', 'I'm going to hurt myself', or 'I want to die'.\n\n"
                    "Answer NO for: metaphorical language ('I want to disappear', "
                    "'I make myself smaller', 'I feel invisible'), relational withdrawal "
                    "('I go quiet', 'I shut down'), frustration with others "
                    "('I could kill him'), or general hopelessness without explicit intent.\n\n"
                    "Answer YES or NO only.\n\n"
                    f"Message: {text[:500]}"
                ),
            }],
        )
        answer = result.content[0].text.strip().upper()
        if answer.startswith("YES"):
            logger.warning("Claude crisis check flagged contextual crisis language.")
            return True
        return False
    except Exception as exc:
        logger.warning("Claude crisis check failed (%s); skipping secondary check.", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_NAME_HINT = (
    "\n\n"
    "One quick note: you're shown by a generic, anonymous name for now. "
    "If you'd like to personalize it, click the pencil icon next to your name at the top right — "
    "any name not already used in the past for this session works fine."
)


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
        return response.content[0].text + _NAME_HINT
    except Exception as exc:
        logger.warning("Opening message generation failed (%s); using fallback.", exc)
        return "Hello, and welcome. What's brought you here today?" + _NAME_HINT


def generate_silence_nudge(mode: str, history: list = None) -> str:
    """Generate a brief re-engagement nudge after a period of total silence.

    Used in couple/group sessions when no one has spoken for a while. Should be
    short (1-2 sentences), gentle, open-ended, and reference the conversation
    so it feels grounded rather than generic. Falls back to a simple line if
    the API is unavailable.
    """
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        mode_context=_MODE_CONTEXT.get(mode, _MODE_CONTEXT["solo"]),
    )
    nudge_instruction = (
        "[Silence check-in] The participants have been quiet for a while. "
        "Send a brief, gentle re-engagement message — 1 to 2 sentences, no questions stacked, "
        "open-ended. Reference what was just discussed if it helps it feel grounded. "
        "Do NOT recap the whole conversation. Do NOT introduce a new topic. "
        "Tone: warm, unhurried, not pushy. Examples of the right register: "
        "\"Take your time — I'm here when you're ready.\" or "
        "\"No rush. Whenever something comes up, I'm listening.\""
    )
    try:
        client = _get_claude_client()
        messages = []
        if history:
            messages.extend(history[-10:])  # last 10 turns for context, capped
        messages.append({"role": "user", "content": nudge_instruction})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
        )
        return response.content[0].text
    except Exception as exc:
        logger.warning("Silence nudge generation failed (%s); using fallback.", exc)
        return "Take your time — I'm here whenever you're ready to continue."


def process_input(
    text: str,
    mode: str = "solo",
    session_message_count: int = 0,
    history: list = None,
    referral_already_made: bool = False,
) -> str:
    """Return a context-aware therapeutic response for the given user input.

    Pipeline
    --------
    1a. Crisis keyword check  → return CRISIS_RESPONSE immediately (fast, no API call).
    1b. Claude crisis check   → contextual check for phrases keywords miss.
    2.  Emotion classification → j-hartmann/emotion-english-distilroberta-base (CPU).
    3.  Claude Sonnet 4.6     → generates the response with the emotion as context.
    4.  Medical output guard  → keyword + Claude check for medical/diagnostic language.
    """
    # 1a. Crisis check Layer 1 — keywords (zero latency)
    if detect_crisis(text):
        return CRISIS_RESPONSE

    # 1b. Crisis check Layer 2 — Claude contextual check for phrases keywords miss
    if _claude_crisis_check(text):
        return CRISIS_RESPONSE

    # 2. Emotion detection
    emotion = analyze_emotion(text)
    sentiment = EMOTION_TO_SENTIMENT.get(emotion, "neutral")

    # 2b. Off-topic pre-filter — catch obvious factual questions before calling Claude
    if emotion == "neutral" and _looks_like_factual_question(text):
        return OFFTOPIC_SAFE_RESPONSE

    # 3. Escalation flag
    needs_escalation = (
        detect_escalation(text)
        or (sentiment == "negative" and session_message_count >= 10)
    )

    # 4. Generate response via Claude
    response = _generate_claude_response(
        text, emotion, mode, needs_escalation,
        history=history,
        referral_already_made=referral_already_made,
    )

    # 5. Medical output guard (keyword + Claude)
    return _medical_guard(response)
