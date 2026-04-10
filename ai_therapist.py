import random

# ---------------------------------------------------------------------------
# Crisis keywords — any match stops normal flow and triggers emergency response
# ---------------------------------------------------------------------------

CRISIS_KEYWORDS = {
    "suicide", "suicidal", "kill myself", "end my life", "want to die",
    "don't want to live", "no reason to live", "self-harm", "self harm",
    "cutting myself", "hurt myself", "hurting myself", "harm myself",
    "overdose", "take my own life", "end it all", "better off dead",
    "not worth living", "rather be dead", "wish i was dead",
}

# Triggers a human-therapist referral appended to the normal response
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

# ---------------------------------------------------------------------------
# Crisis response — replaces normal response entirely
# ---------------------------------------------------------------------------

CRISIS_RESPONSE = (
    "I'm very concerned about what you've shared and I want to make sure you are safe. "
    "This AI is not equipped to support a crisis.\n\n"
    "If you are in immediate danger, please call 911 (or your local emergency number).\n\n"
    "Free, confidential crisis support available 24/7:\n"
    "• Call or text 988 — Suicide & Crisis Lifeline (US)\n"
    "• Text HOME to 741741 — Crisis Text Line (US)\n"
    "• findahelpline.com — international directory\n\n"
    "Please reach out to a licensed human therapist or counselor. "
    "You deserve real, professional support — not an AI."
)

# Appended to normal responses when escalation is detected
HUMAN_REFERRAL_NOTE = (
    "\n\nI want to be transparent: I am an AI, not a licensed therapist, "
    "and I have real limitations. What you are describing may benefit from "
    "the support of a qualified human professional. "
    "Psychology Today's therapist finder (psychologytoday.com/us/therapists) "
    "is a good place to start. You do not have to navigate this alone."
)

# ---------------------------------------------------------------------------
# Mode-aware response banks
# ---------------------------------------------------------------------------

SOLO_NEGATIVE = [
    "It sounds like you're carrying something heavy right now. "
    "Let's try a CBT technique — can you identify one specific thought that's troubling you most?",
    "I hear how tough this is. Would you like to try a short mindfulness exercise together?",
    "Your feelings are completely valid. Take a slow breath with me — in for 4, hold for 4, out for 4. "
    "What's one small thing you can do for yourself today?",
    "It takes real courage to share that. Let's break it down — what feels most pressing to you right now?",
]

SOLO_NEUTRAL = [
    "Thanks for sharing. What's one small, positive step you'd like to take today?",
    "Let's reflect — is there anything, even something small, that felt okay today?",
    "You showed up, and that matters. What has been on your mind most lately?",
    "Sometimes neutral is a solid place to stand. What would help you feel more grounded right now?",
]

SOLO_POSITIVE = [
    "That's wonderful to hear! What do you think has been contributing to this feeling?",
    "You're doing great — want to set a small goal to keep this momentum going?",
    "Hold onto this positive energy. What's one thing you're most grateful for today?",
    "Celebrating moments like this is so important. What would you like to build toward next?",
]

COUPLE_NEGATIVE = [
    "It sounds like there are some difficult feelings present between you two. "
    "I'd encourage each of you to share one specific feeling using an 'I feel…' statement, "
    "so the other person can truly hear you without feeling blamed.",
    "Strong emotions in couples work are normal and expected. Before going further, "
    "can each of you take a breath and name the one thing you most want your partner to understand right now?",
    "It can be hard to feel heard when emotions are running high. "
    "What would it look like for both of you to feel genuinely understood in this moment?",
    "Let's slow down together. Each of you — what is one feeling that's present for you right now, "
    "and what do you need from your partner?",
]

COUPLE_NEUTRAL = [
    "Welcome, both of you. How are you each feeling as you come into this session today? "
    "A brief check-in from each of you helps set the tone.",
    "Connecting like this takes intention. What brought the two of you here today?",
    "Let's start somewhere positive — can each of you share one thing you appreciate about the other this week?",
    "What's one shared goal you'd both like to focus on in this session together?",
]

COUPLE_POSITIVE = [
    "It's great to hear some positive energy between you two. "
    "What do you think you've both been doing well lately as a couple?",
    "This is a wonderful foundation to build from. "
    "What's one thing each of you would like to continue or strengthen together?",
    "Acknowledging growth as a couple is powerful. "
    "What milestone — big or small — would you both like to celebrate today?",
    "Wonderful! What does each of you feel has contributed most to this positive place you're in?",
]

GROUP_NEGATIVE = [
    "It sounds like some heavy feelings are present in the group right now. "
    "Would anyone like to share what's coming up for them, "
    "knowing the group is here to listen — not judge?",
    "These feelings are real and they matter. "
    "As a group, let's hold space for each other. "
    "Does anyone want to respond with compassion to what's been shared?",
    "Pain that is shared in a group often feels a little lighter. "
    "I invite anyone who's comfortable to gently reflect on what they've heard.",
    "Let's take a collective breath together. "
    "What does the group need most right now — to be heard, to reflect, or to find a next step forward?",
]

GROUP_NEUTRAL = [
    "Welcome, everyone. Let's start with a quick check-in — "
    "how is each person feeling as they arrive today?",
    "Group work is powerful. What is something each of you is hoping to take away from today's session?",
    "Let's build some shared awareness — what's one word that describes where you are emotionally right now?",
    "Being here together already matters. "
    "What's one thing the group could do to better support each other today?",
]

GROUP_POSITIVE = [
    "It's wonderful to hear positive energy in the group! "
    "What has the group been doing well that may be contributing to this?",
    "Let's celebrate this together. Would anyone like to share something that's going well for them?",
    "This positivity is a resource for everyone here. "
    "What's one way each of you would like to carry this feeling forward?",
    "Great energy today. What shared goal would the group like to focus on to keep building on this?",
]

RESPONSE_BANKS = {
    "solo":   {"negative": SOLO_NEGATIVE,   "neutral": SOLO_NEUTRAL,   "positive": SOLO_POSITIVE},
    "couple": {"negative": COUPLE_NEGATIVE, "neutral": COUPLE_NEUTRAL, "positive": COUPLE_POSITIVE},
    "group":  {"negative": GROUP_NEGATIVE,  "neutral": GROUP_NEUTRAL,  "positive": GROUP_POSITIVE},
}

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


def analyze_sentiment(text: str) -> str:
    """Classify text as 'negative', 'positive', or 'neutral' via keyword matching."""
    words = set(text.lower().split())
    negative_count = len(words & NEGATIVE_KEYWORDS)
    positive_count = len(words & POSITIVE_KEYWORDS)
    if negative_count > positive_count:
        return "negative"
    elif positive_count > negative_count:
        return "positive"
    return "neutral"


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
    """
    Guard against any response that inadvertently contains diagnostic or
    prescriptive language. Falls back to a safe neutral response.
    """
    lowered = response.lower()
    if any(phrase in lowered for phrase in _FORBIDDEN_OUTPUT_PHRASES):
        return random.choice(SOLO_NEUTRAL)
    return response


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_input(text: str, mode: str = "solo", session_message_count: int = 0) -> str:
    """
    Return a context-aware therapeutic response for the given user input.

    Parameters
    ----------
    text : str
        The user's message.
    mode : str
        Therapy mode — 'solo', 'couple', or 'group'. Controls response framing.
    session_message_count : int
        Total messages already in the session. Used to detect sustained distress
        and trigger a human-therapist referral after prolonged negative sessions.

    Behaviour
    ---------
    1. Crisis language detected → return CRISIS_RESPONSE immediately (no normal reply).
    2. Escalation keywords or sustained negative sessions (≥10 messages) →
       append HUMAN_REFERRAL_NOTE to the normal response.
    3. All other cases → mode-aware response selected from the appropriate bank.
    """
    # 1. Crisis check — always takes priority, stops all other processing
    if detect_crisis(text):
        return CRISIS_RESPONSE

    # 2. Sentiment + mode-aware response
    sentiment = analyze_sentiment(text)
    bank = RESPONSE_BANKS.get(mode, RESPONSE_BANKS["solo"])
    response = random.choice(bank[sentiment])

    # 3. Escalation: append human referral when topic exceeds AI capability
    #    or when the user has been in sustained distress for a long session
    if detect_escalation(text) or (sentiment == "negative" and session_message_count >= 10):
        response += HUMAN_REFERRAL_NOTE

    # 4. Output guard — strip any diagnostic/prescriptive language (defensive)
    return _sanitize_response(response)
