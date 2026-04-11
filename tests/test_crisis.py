import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ai_therapist import detect_crisis, analyze_sentiment, process_input, _sanitize_response


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
    # "suicide" must appear as substring — "suicidal" contains it so should match
    assert detect_crisis("I feel suicidal") is True


def test_analyze_sentiment_negative():
    assert analyze_sentiment("I feel sad and depressed") == "negative"


def test_analyze_sentiment_positive():
    assert analyze_sentiment("I feel happy and grateful") == "positive"


def test_analyze_sentiment_neutral():
    assert analyze_sentiment("I went to the store today") == "neutral"


def test_analyze_sentiment_tie_returns_neutral():
    # One negative and one positive keyword — tie → neutral
    assert analyze_sentiment("I feel sad but also happy") == "neutral"


def test_process_input_crisis_contains_hotline():
    result = process_input("I want to kill myself")
    assert "988" in result


def test_process_input_crisis_contains_concern():
    result = process_input("I want to end my life")
    assert "concerned" in result.lower() or "crisis" in result.lower()


def test_process_input_escalation_appends_referral():
    result = process_input("I need a therapist for my trauma", mode="solo")
    assert "licensed" in result.lower() or "professional" in result.lower()


def test_process_input_sustained_negative_appends_referral():
    # 10+ messages with negative sentiment should trigger referral
    result = process_input("I feel sad and overwhelmed", mode="solo", session_message_count=10)
    assert "licensed" in result.lower() or "professional" in result.lower()


def test_sanitize_strips_diagnostic_language():
    result = _sanitize_response("you have depression and you should take medication")
    assert "you have depression" not in result.lower()


def test_sanitize_passes_clean_response():
    clean = "Let's take a moment to reflect on how you're feeling."
    assert _sanitize_response(clean) == clean
