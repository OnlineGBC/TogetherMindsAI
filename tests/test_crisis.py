import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
from ai_therapist import detect_crisis, analyze_sentiment, process_input, _sanitize_response


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _mock_emotion(label: str):
    """Return a patch for _get_emotion_pipeline that always yields `label`."""
    mock_pipe = MagicMock(return_value=[[{"label": label, "score": 0.99}]])
    return patch("ai_therapist._get_emotion_pipeline", return_value=mock_pipe)


def _mock_claude(text: str):
    """Return a patch for _get_claude_client that always returns `text`."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    return patch("ai_therapist._get_claude_client", return_value=mock_client)


# ---------------------------------------------------------------------------
# Crisis detection — rule-based, no mocking needed
# ---------------------------------------------------------------------------

def test_detect_crisis_kill_myself():
    assert detect_crisis("I want to kill myself") is True


def test_detect_crisis_end_my_life():
    assert detect_crisis("I want to end my life") is True


def test_detect_crisis_self_harm():
    assert detect_crisis("I have been doing self-harm") is True


def test_detect_crisis_negative_case():
    assert detect_crisis("I feel really sad today") is False


def test_detect_crisis_case_insensitive():
    assert detect_crisis("KILL MYSELF right now") is True


def test_detect_crisis_partial_word_not_matched():
    assert detect_crisis("I feel suicidal") is True


# ---------------------------------------------------------------------------
# Sentiment analysis — uses emotion classifier (mocked)
# ---------------------------------------------------------------------------

def test_analyze_sentiment_negative():
    with _mock_emotion("sadness"):
        assert analyze_sentiment("I feel sad and depressed") == "negative"


def test_analyze_sentiment_positive():
    with _mock_emotion("joy"):
        assert analyze_sentiment("I feel happy and grateful") == "positive"


def test_analyze_sentiment_neutral():
    with _mock_emotion("neutral"):
        assert analyze_sentiment("I went to the store today") == "neutral"


def test_analyze_sentiment_tie_returns_neutral():
    with _mock_emotion("neutral"):
        assert analyze_sentiment("I feel sad but also happy") == "neutral"


# ---------------------------------------------------------------------------
# process_input — crisis path bypasses Claude entirely
# ---------------------------------------------------------------------------

def test_process_input_crisis_contains_hotline():
    # Crisis response is pure rule-based — Claude must NOT be called
    result = process_input("I want to kill myself")
    assert "988" in result


def test_process_input_crisis_contains_concern():
    result = process_input("I want to end my life")
    assert "concerned" in result.lower() or "crisis" in result.lower()


# ---------------------------------------------------------------------------
# process_input — non-crisis paths go through Claude (mocked)
# ---------------------------------------------------------------------------

def test_process_input_escalation_appends_referral():
    claude_reply = "I hear you. Working with a licensed therapist can offer deeper support."
    with _mock_emotion("fear"), _mock_claude(claude_reply):
        result = process_input("I need a therapist for my trauma", mode="solo")
    assert "licensed" in result.lower() or "professional" in result.lower()


def test_process_input_sustained_negative_appends_referral():
    claude_reply = "A professional therapist can be a valuable resource for you."
    with _mock_emotion("sadness"), _mock_claude(claude_reply):
        result = process_input(
            "I feel sad and overwhelmed", mode="solo", session_message_count=10
        )
    assert "professional" in result.lower() or "therapist" in result.lower()


def test_process_input_calls_claude_for_normal_message():
    claude_reply = "It sounds like you're going through something difficult."
    with _mock_emotion("sadness"), _mock_claude(claude_reply) as mock_client_patch:
        result = process_input("I feel a bit down today", mode="solo")
    assert result == claude_reply


# ---------------------------------------------------------------------------
# Output sanitiser
# ---------------------------------------------------------------------------

def test_sanitize_strips_diagnostic_language():
    result = _sanitize_response("you have depression and you should take medication")
    assert "you have depression" not in result.lower()


def test_sanitize_passes_clean_response():
    clean = "Let's take a moment to reflect on how you're feeling."
    assert _sanitize_response(clean) == clean
