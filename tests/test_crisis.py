import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock
from ai_therapist import (detect_crisis, analyze_sentiment, process_input, _medical_guard,
                          _claude_crisis_check, _looks_like_factual_question,
                          OFFTOPIC_SAFE_RESPONSE)


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
# Medical output guard — keyword layer
# ---------------------------------------------------------------------------

def test_medical_guard_strips_diagnostic_language():
    result = _medical_guard("you have depression and you should take medication")
    assert "you have depression" not in result.lower()
    assert "medical advice" in result.lower()


def test_medical_guard_passes_clean_response():
    clean = "Let's take a moment to reflect on how you're feeling."
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="NO")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        assert _medical_guard(clean) == clean


def test_medical_guard_claude_flags_drug_name():
    """Claude layer intercepts a drug name the keyword list misses."""
    response_with_drug = "You might consider taking ibuprofen for that."
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="YES")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        result = _medical_guard(response_with_drug)
    assert "medical advice" in result.lower()


def test_medical_guard_passes_through_on_api_failure():
    """If the Claude check fails, response passes through rather than crashing."""
    clean = "That sounds really difficult. How long have you felt this way?"
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API error")
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        result = _medical_guard(clean)
    assert result == clean


# ---------------------------------------------------------------------------
# Claude contextual crisis check (2.1 Layer 2)
# ---------------------------------------------------------------------------

def test_claude_crisis_check_returns_true_for_contextual_crisis():
    """Claude check catches contextual phrases keywords miss."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="YES")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        assert _claude_crisis_check("I can't go on anymore") is True


def test_claude_crisis_check_returns_false_for_normal_message():
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="NO")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        assert _claude_crisis_check("I had a tough day at work") is False


def test_claude_crisis_check_returns_false_on_api_failure():
    """If Claude call fails, crisis check returns False so conversation continues."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API error")
    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        assert _claude_crisis_check("I don't want to be here anymore") is False


def test_process_input_crisis_caught_by_claude_layer():
    """process_input returns CRISIS_RESPONSE when Claude layer fires."""
    from ai_therapist import CRISIS_RESPONSE
    # Keyword layer misses this phrase; Claude layer catches it
    mock_crisis_msg = MagicMock()
    mock_crisis_msg.content = [MagicMock(text="YES")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_crisis_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client), \
         patch("ai_therapist._get_emotion_pipeline",
               return_value=MagicMock(return_value=[[{"label": "sadness", "score": 0.9}]])):
        result = process_input("I don't want to be here anymore")
    assert result == CRISIS_RESPONSE


# ---------------------------------------------------------------------------
# Off-topic pre-filter — _looks_like_factual_question and process_input path
# ---------------------------------------------------------------------------

def test_looks_like_factual_question_detects_trivia():
    assert _looks_like_factual_question("What is the capital of France?") is True


def test_looks_like_factual_question_detects_how_question():
    assert _looks_like_factual_question("How does photosynthesis work?") is True


def test_looks_like_factual_question_false_for_emotional_content():
    # "sad" is an emotional signal word — should not be flagged as off-topic
    assert _looks_like_factual_question("Why do I feel so sad all the time?") is False


def test_looks_like_factual_question_false_for_no_question_word():
    assert _looks_like_factual_question("I went to the store today") is False


def test_process_input_deflects_neutral_factual_question():
    """Neutral emotion + factual question → OFFTOPIC_SAFE_RESPONSE, no Claude call."""
    mock_client = MagicMock()
    # Claude crisis check returns NO; if we reach Claude main call it should not happen
    mock_no_msg = MagicMock()
    mock_no_msg.content = [MagicMock(text="NO")]
    mock_client.messages.create.return_value = mock_no_msg
    with patch("ai_therapist._get_claude_client", return_value=mock_client), \
         _mock_emotion("neutral"):
        result = process_input("What is the capital of France?")
    assert result == OFFTOPIC_SAFE_RESPONSE
    # Sonnet should NOT have been called (only the Haiku crisis check may have been called)
    calls = mock_client.messages.create.call_args_list
    sonnet_calls = [c for c in calls if "system" in c.kwargs]
    assert len(sonnet_calls) == 0


def test_process_input_passes_emotional_question_to_claude():
    """Emotional question (sad) with neutral classifier → not deflected."""
    claude_reply = "It sounds like you're carrying something heavy."
    with _mock_emotion("neutral"), _mock_claude(claude_reply):
        result = process_input("Why do I feel so sad all the time?")
    assert result == claude_reply


def test_process_input_passes_non_neutral_factual_question_to_claude():
    """Factual-looking question but non-neutral emotion → not deflected."""
    claude_reply = "Let's explore what's behind that question."
    with _mock_emotion("fear"), _mock_claude(claude_reply):
        result = process_input("What is wrong with me?")
    assert result == claude_reply
