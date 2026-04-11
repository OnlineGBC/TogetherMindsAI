"""
test_ai_therapist.py
--------------------
Tests for the two-stage AI pipeline:
  - Emotion classifier (HuggingFace, mocked)
  - Claude Sonnet response generator (mocked)
  - Fallback behaviour when Claude is unavailable
  - Integration of both stages through process_input
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch, MagicMock, call
import ai_therapist
from ai_therapist import (
    analyze_emotion,
    analyze_sentiment,
    process_input,
    detect_crisis,
    detect_escalation,
    EMOTION_TO_SENTIMENT,
    CRISIS_RESPONSE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_emotion_pipe(label: str, score: float = 0.95):
    return MagicMock(return_value=[[{"label": label, "score": score}]])


def _make_claude_client(reply: str):
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=reply)]
    client = MagicMock()
    client.messages.create.return_value = mock_msg
    return client


# ---------------------------------------------------------------------------
# Emotion classifier
# ---------------------------------------------------------------------------

class TestEmotionClassifier:

    def test_returns_lowercase_emotion_label(self):
        pipe = _make_emotion_pipe("Sadness")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe):
            assert analyze_emotion("I am very sad") == "sadness"

    def test_anger_maps_to_negative_sentiment(self):
        pipe = _make_emotion_pipe("anger")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe):
            assert analyze_sentiment("I am furious") == "negative"

    def test_fear_maps_to_negative_sentiment(self):
        pipe = _make_emotion_pipe("fear")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe):
            assert analyze_sentiment("I am terrified") == "negative"

    def test_joy_maps_to_positive_sentiment(self):
        pipe = _make_emotion_pipe("joy")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe):
            assert analyze_sentiment("I feel fantastic") == "positive"

    def test_surprise_maps_to_neutral_sentiment(self):
        pipe = _make_emotion_pipe("surprise")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe):
            assert analyze_sentiment("That was unexpected") == "neutral"

    def test_classifier_failure_falls_back_to_keyword(self):
        broken_pipe = MagicMock(side_effect=RuntimeError("model not loaded"))
        with patch("ai_therapist._get_emotion_pipeline", return_value=broken_pipe):
            # "sad" is a negative keyword — fallback should catch it
            result = analyze_emotion("I feel sad")
        assert result in EMOTION_TO_SENTIMENT

    def test_all_emotion_labels_covered_in_map(self):
        expected = {"anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise"}
        assert expected == set(EMOTION_TO_SENTIMENT.keys())


# ---------------------------------------------------------------------------
# Claude response generator
# ---------------------------------------------------------------------------

class TestClaudeGenerator:

    def test_claude_response_returned_for_normal_input(self):
        client = _make_claude_client("How are you feeling right now?")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            result = process_input("Just checking in", mode="solo")
        assert result == "How are you feeling right now?"

    def test_claude_called_with_emotion_context(self):
        client = _make_claude_client("I hear you.")
        pipe = _make_emotion_pipe("sadness")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I feel down", mode="solo")
        call_kwargs = client.messages.create.call_args
        user_content = call_kwargs.kwargs["messages"][0]["content"]
        assert "sadness" in user_content

    def test_prompt_caching_applied_to_system(self):
        client = _make_claude_client("Ok.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello", mode="solo")
        system_arg = client.messages.create.call_args.kwargs["system"]
        assert isinstance(system_arg, list)
        assert system_arg[0]["cache_control"] == {"type": "ephemeral"}

    def test_uses_claude_sonnet_model(self):
        client = _make_claude_client("Hi.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello", mode="solo")
        model = client.messages.create.call_args.kwargs["model"]
        assert "sonnet" in model

    def test_escalation_hint_injected_when_needed(self):
        client = _make_claude_client("Consider speaking to a professional.")
        pipe = _make_emotion_pipe("fear")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I need a therapist", mode="solo")
        user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "escalation" in user_content.lower() or "professional" in user_content.lower()

    def test_no_escalation_hint_for_normal_message(self):
        client = _make_claude_client("That's great!")
        pipe = _make_emotion_pipe("joy")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I feel good today", mode="solo", session_message_count=0)
        user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Internal note" not in user_content

    def test_couple_mode_system_prompt_mentions_partners(self):
        client = _make_claude_client("Let's hear from both of you.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("We are struggling", mode="couple")
        system_text = client.messages.create.call_args.kwargs["system"][0]["text"]
        assert "couple" in system_text.lower() or "partner" in system_text.lower()

    def test_group_mode_system_prompt_mentions_group(self):
        client = _make_claude_client("Welcome everyone.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello group", mode="group")
        system_text = client.messages.create.call_args.kwargs["system"][0]["text"]
        assert "group" in system_text.lower()


# ---------------------------------------------------------------------------
# Fallback behaviour when Claude API fails
# ---------------------------------------------------------------------------

class TestClaudeFallback:

    def test_fallback_used_when_claude_raises(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("API down")
        pipe = _make_emotion_pipe("sadness")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            result = process_input("I feel awful", mode="solo")
        # Should not raise — should return a non-empty fallback string
        assert isinstance(result, str)
        assert len(result) > 10

    def test_fallback_string_is_appropriate_for_sentiment(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("timeout")
        pipe = _make_emotion_pipe("joy")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            result = process_input("I feel great!", mode="solo")
        assert isinstance(result, str)
        assert len(result) > 10


# ---------------------------------------------------------------------------
# Crisis path — Claude must never be called
# ---------------------------------------------------------------------------

class TestCrisisBypass:

    def test_claude_not_called_on_crisis(self):
        client = MagicMock()
        pipe = _make_emotion_pipe("fear")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            result = process_input("I want to kill myself")
        client.messages.create.assert_not_called()
        assert "988" in result

    def test_emotion_classifier_not_called_on_crisis(self):
        pipe = _make_emotion_pipe("sadness")
        client = _make_claude_client("some response")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe) as mock_pipe_getter, \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I want to end my life")
        # The pipeline getter should not have been called
        mock_pipe_getter.assert_not_called()

    def test_crisis_response_exact_content(self):
        result = process_input("I want to kill myself")
        assert result == CRISIS_RESPONSE
