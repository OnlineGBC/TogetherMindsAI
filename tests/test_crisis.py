import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from ai_therapist import detect_crisis, detect_escalation


# ---------------------------------------------------------------------------
# Crisis detection — rule-based keyword pre-filter (kept; drives the co-pilot's
# client-facing safety net + risk cards)
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


def test_detect_crisis_suicidal():
    assert detect_crisis("I feel suicidal") is True


# ---------------------------------------------------------------------------
# Escalation detection — used by the co-pilot to surface risk cards
# ---------------------------------------------------------------------------

def test_detect_escalation_medication():
    assert detect_escalation("I think I need medication for this") is True


def test_detect_escalation_trauma():
    assert detect_escalation("this is about my trauma") is True


def test_detect_escalation_negative_case():
    assert detect_escalation("I had a pleasant walk in the park today") is False
