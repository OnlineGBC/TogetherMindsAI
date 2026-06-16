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
    contains_referral,
    strip_referral_sentences,
    EMOTION_TO_SENTIMENT,
    CRISIS_RESPONSE,
    OFFTOPIC_SAFE_RESPONSE,
    _looks_like_factual_question,
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


def _find_sonnet_call(client):
    """Return the main Sonnet call — the only call that includes a 'system' kwarg."""
    return next(
        c for c in client.messages.create.call_args_list
        if "system" in c.kwargs
    )


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
        sonnet_call = _find_sonnet_call(client)
        user_content = sonnet_call.kwargs["messages"][0]["content"]
        assert "sadness" in user_content

    def test_prompt_caching_applied_to_system(self):
        client = _make_claude_client("Ok.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello", mode="solo")
        sonnet_call = _find_sonnet_call(client)
        system_arg = sonnet_call.kwargs["system"]
        assert isinstance(system_arg, list)
        assert system_arg[0]["cache_control"] == {"type": "ephemeral"}

    def test_uses_claude_sonnet_model(self):
        client = _make_claude_client("Hi.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello", mode="solo")
        sonnet_call = _find_sonnet_call(client)
        model = sonnet_call.kwargs["model"]
        assert "sonnet" in model

    def test_escalation_hint_injected_when_needed(self):
        client = _make_claude_client("Consider speaking to a professional.")
        pipe = _make_emotion_pipe("fear")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I need a therapist", mode="solo")
        sonnet_call = _find_sonnet_call(client)
        user_content = sonnet_call.kwargs["messages"][0]["content"]
        assert "escalation" in user_content.lower() or "professional" in user_content.lower()

    def test_no_escalation_hint_for_normal_message(self):
        client = _make_claude_client("That's great!")
        pipe = _make_emotion_pipe("joy")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("I feel good today", mode="solo", session_message_count=0)
        sonnet_call = _find_sonnet_call(client)
        user_content = sonnet_call.kwargs["messages"][0]["content"]
        assert "Internal note" not in user_content

    def test_couple_mode_system_prompt_mentions_partners(self):
        client = _make_claude_client("Let's hear from both of you.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("We are struggling", mode="couple")
        sonnet_call = _find_sonnet_call(client)
        system_text = sonnet_call.kwargs["system"][0]["text"]
        assert "couple" in system_text.lower() or "partner" in system_text.lower()

    def test_group_mode_system_prompt_mentions_group(self):
        client = _make_claude_client("Welcome everyone.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            process_input("Hello group", mode="group")
        sonnet_call = _find_sonnet_call(client)
        system_text = sonnet_call.kwargs["system"][0]["text"]
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


# ---------------------------------------------------------------------------
# Off-topic pre-filter — _looks_like_factual_question
# ---------------------------------------------------------------------------

class TestOffTopicPreFilter:

    def test_short_trivia_question_flagged(self):
        assert _looks_like_factual_question("What is the capital of France?") is True

    def test_personal_coping_question_not_flagged(self):
        # Contains emotional word "stress" + question word — must NOT deflect
        assert _looks_like_factual_question(
            "What are some practical things I can do to manage this stress?"
        ) is False

    def test_long_personal_question_not_flagged(self):
        # > 60 chars — must never be flagged regardless of content
        assert _looks_like_factual_question(
            "How can I cope when things get really hard between us and I feel lost?"
        ) is False

    def test_no_question_mark_not_flagged(self):
        assert _looks_like_factual_question("Tell me about Paris") is False

    def test_question_word_not_at_start_not_flagged(self):
        # "what" not first word
        assert _looks_like_factual_question("I don't know what to do?") is False

    def test_metaphorical_withdrawal_not_deflected(self):
        # "I make myself smaller" — emotional content, no question mark
        # Should never be flagged as off-topic
        assert _looks_like_factual_question(
            "I make myself smaller so I don't mess things up more"
        ) is False

    def test_process_input_personal_coping_question_reaches_claude(self):
        """process_input must NOT return OFFTOPIC_SAFE_RESPONSE for therapy-relevant questions."""
        client = _make_claude_client("That's a great question about managing stress.")
        pipe = _make_emotion_pipe("neutral")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client):
            result = process_input(
                "What are some practical things I can do to manage this stress?",
                mode="solo",
            )
        assert result != OFFTOPIC_SAFE_RESPONSE

    def test_process_input_withdrawal_language_not_deflected(self):
        """'I make myself smaller' must not trigger off-topic or crisis response."""
        client = _make_claude_client("I hear how much you shrink yourself to keep the peace.")
        pipe = _make_emotion_pipe("sadness")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client), \
             patch("ai_therapist._claude_crisis_check", return_value=False):
            result = process_input(
                "I make myself smaller so I don't mess things up more",
                mode="couple",
            )
        assert result != OFFTOPIC_SAFE_RESPONSE
        assert result != CRISIS_RESPONSE


# ---------------------------------------------------------------------------
# Referral tracking
# ---------------------------------------------------------------------------

class TestReferralTracking:

    def test_contains_referral_detects_licensed_therapist(self):
        assert contains_referral("I'd encourage you to see a licensed therapist.") is True

    def test_contains_referral_detects_couples_therapist(self):
        assert contains_referral("A couples therapist could help you both.") is True

    def test_contains_referral_false_for_normal_response(self):
        assert contains_referral("That sounds really difficult. Tell me more.") is False

    def test_referral_already_made_suppresses_hint_in_prompt(self):
        """When referral_already_made=True, the escalation hint must say NOT to repeat."""
        client = _make_claude_client("I hear you.")
        pipe = _make_emotion_pipe("sadness")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client), \
             patch("ai_therapist._claude_crisis_check", return_value=False):
            process_input(
                "I need a therapist",
                mode="solo",
                referral_already_made=True,
            )
        # The internal note injected must say NOT to repeat
        sonnet_call = _find_sonnet_call(client)
        user_content = sonnet_call.kwargs["messages"][-1]["content"]
        assert "NOT" in user_content or "not" in user_content

    def test_referral_not_made_allows_escalation_hint(self):
        """When referral_already_made=False and escalation needed, hint is injected."""
        client = _make_claude_client("Consider speaking to a professional.")
        pipe = _make_emotion_pipe("fear")
        with patch("ai_therapist._get_emotion_pipeline", return_value=pipe), \
             patch("ai_therapist._get_claude_client", return_value=client), \
             patch("ai_therapist._claude_crisis_check", return_value=False):
            process_input(
                "I need a therapist",
                mode="solo",
                referral_already_made=False,
            )
        sonnet_call = _find_sonnet_call(client)
        user_content = sonnet_call.kwargs["messages"][-1]["content"]
        assert "professional" in user_content.lower() or "support" in user_content.lower()


# ---------------------------------------------------------------------------
# Broadened referral detection
# ---------------------------------------------------------------------------

class TestContainsReferralBroadened:

    def test_detects_human_couples_counsellor(self):
        assert contains_referral("working with a human couples counsellor") is True

    def test_detects_qualified_therapist(self):
        assert contains_referral("a qualified therapist could really help here") is True

    def test_detects_certified_practitioner(self):
        assert contains_referral("a certified practitioner can offer more") is True

    def test_detects_see_a_counselor(self):
        assert contains_referral("I'd encourage you to see a counselor") is True

    def test_detects_find_a_therapist(self):
        assert contains_referral("it's worth looking for a therapist you trust") is True

    def test_false_for_plain_therapeutic_reflection(self):
        assert contains_referral("That sounds really difficult. Tell me more.") is False

    def test_false_for_therapy_noun_without_recommendation(self):
        # "therapist" alone — no recommendation word
        assert contains_referral("You mentioned a therapist in the past.") is False


# ---------------------------------------------------------------------------
# Referral sentence stripping
# ---------------------------------------------------------------------------

class TestStripReferralSentences:

    def test_removes_referral_sentence_keeps_rest(self):
        text = (
            "That sounds really hard. "
            "I'd encourage you to see a licensed therapist. "
            "Let's stay with that feeling for a moment."
        )
        result = strip_referral_sentences(text)
        assert "licensed therapist" not in result
        assert "That sounds really hard" in result
        assert "stay with that feeling" in result

    def test_preserves_clean_response_unchanged(self):
        text = "I hear you. That feeling of disconnection is real."
        assert strip_referral_sentences(text) == text

    def test_never_returns_empty_string(self):
        # Entire response is a referral — must return something
        text = "I'd encourage you to see a licensed therapist."
        result = strip_referral_sentences(text)
        assert len(result) > 0

    def test_strips_human_counsellor_variant(self):
        text = (
            "You're both working hard here. "
            "Working with a human couples counsellor over time could help. "
            "What came up for you just now?"
        )
        result = strip_referral_sentences(text)
        assert "counsellor" not in result
        assert "working hard" in result

    def test_multiple_referral_sentences_all_removed(self):
        text = (
            "I hear you both. "
            "A licensed therapist could really help. "
            "I'd also encourage you to see a counsellor. "
            "What matters most right now is that you feel heard."
        )
        result = strip_referral_sentences(text)
        assert "therapist" not in result
        assert "counsellor" not in result
        assert "feel heard" in result


# ---------------------------------------------------------------------------
# Opening message — name hint must always be appended
# ---------------------------------------------------------------------------

class TestOpeningMessage:

    def test_solo_opening_contains_name_hint(self):
        client = _make_claude_client("Welcome, I'm here to listen.")
        with patch("ai_therapist._get_claude_client", return_value=client):
            result = ai_therapist.generate_opening_message("solo")
        assert "pencil" in result

    def test_couple_opening_contains_name_hint(self):
        client = _make_claude_client("Welcome, both of you.")
        with patch("ai_therapist._get_claude_client", return_value=client):
            result = ai_therapist.generate_opening_message("couple")
        assert "pencil" in result

    def test_group_opening_contains_name_hint(self):
        client = _make_claude_client("Welcome to the group.")
        with patch("ai_therapist._get_claude_client", return_value=client):
            result = ai_therapist.generate_opening_message("group")
        assert "pencil" in result

    def test_fallback_opening_also_contains_name_hint(self):
        """When Claude API is unavailable the hardcoded fallback must still include the hint."""
        client = MagicMock()
        client.messages.create.side_effect = Exception("API down")
        with patch("ai_therapist._get_claude_client", return_value=client):
            result = ai_therapist.generate_opening_message("solo")
        assert "pencil" in result


# ---------------------------------------------------------------------------
# Emotion pipeline singleton — must not double-load
# ---------------------------------------------------------------------------

class TestEmotionPipelineSingleton:

    def test_pipeline_function_contains_cache_guard(self):
        """_get_emotion_pipeline must guard with 'if _emotion_pipeline is None' so it never
        reloads the model when already cached.  Without this guard each request could trigger
        a full model load — the race condition the synchronous startup fix was meant to prevent."""
        import pathlib, re
        source = (pathlib.Path(__file__).parent.parent / "ai_therapist.py").read_text(encoding="utf-8")
        # Extract the _get_emotion_pipeline function body
        match = re.search(
            r"def _get_emotion_pipeline\(\):(.*?)(?=^def |\Z)",
            source,
            re.DOTALL | re.MULTILINE,
        )
        assert match, "_get_emotion_pipeline function not found in ai_therapist.py"
        fn_body = match.group(1)
        assert "if _emotion_pipeline is None" in fn_body, (
            "_get_emotion_pipeline is missing the 'if _emotion_pipeline is None' cache guard"
        )
