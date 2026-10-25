"""
tests/test_claude_models.py
---------------------------
Which Claude model each call uses, and reading the reply safely.

Sonnet 5.5 and Haiku 5.5 think by default, so a reply can start with a THINKING
block. The old `response.content[0].text` then raised, the callers swallowed it,
and the co-pilot / summary / translate quietly returned nothing. These tests pin
the fix: one place for model names, thinking turned off on short calls, and a
reader that takes the first TEXT block.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pathlib
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-claude-models")

import ai_therapist
import clinical_summary
import config
import copilot

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _thinking():
    return SimpleNamespace(type="thinking", thinking="")


def _text(value):
    return SimpleNamespace(type="text", text=value)


def _response(*blocks):
    return SimpleNamespace(content=list(blocks))


# --- one place for the model names -----------------------------------------

def test_model_names_come_from_config_with_current_defaults():
    assert config.CLAUDE_MODEL_SMART == os.environ.get("CLAUDE_MODEL_SMART", "claude-sonnet-5-5")
    assert config.CLAUDE_MODEL_FAST == os.environ.get("CLAUDE_MODEL_FAST", "claude-haiku-5-5")


def test_every_call_uses_the_configured_model():
    assert copilot.ADVISOR_MODEL == config.CLAUDE_MODEL_SMART
    assert clinical_summary.SUMMARY_MODEL == config.CLAUDE_MODEL_SMART


@pytest.mark.parametrize("name", ["copilot.py", "clinical_summary.py", "ai_therapist.py",
                                  "TogetherMindsAI.py"])
def test_no_old_model_name_is_hard_coded_in_the_app(name):
    """Opus 4.8 and Sonnet 4.6 were replaced on 2026-10-25. A model name written
    into a call site would quietly bypass the one setting that controls it."""
    source = (ROOT / name).read_text(encoding="utf-8")
    assert "claude-opus-4" not in source
    assert "claude-sonnet-4" not in source


# --- reading the reply ------------------------------------------------------

def test_claude_text_skips_a_thinking_block_that_comes_first():
    assert ai_therapist.claude_text(_response(_thinking(), _text("  EN  "))) == "EN"


def test_claude_text_is_empty_when_there_is_no_text():
    assert ai_therapist.claude_text(_response(_thinking())) == ""
    assert ai_therapist.claude_text(_response()) == ""


def test_claude_text_reads_a_plain_mock_block_like_the_older_tests_use():
    assert ai_therapist.claude_text(_response(MagicMock(text="hello"))) == "hello"


# --- thinking off, with a safe fallback -------------------------------------

def test_thinking_is_turned_off_the_way_each_model_requires():
    assert ai_therapist._thinking_off("claude-haiku-5-5") == {"thinking": {"type": "disabled"}}
    # Sonnet 5.5 rejects "disabled" (HTTP 400); between_tools is its off switch.
    assert ai_therapist._thinking_off("claude-sonnet-5-5") == {"thinking": {"type": "between_tools"}}
    assert ai_therapist._thinking_off("claude-opus-5-5") == {}
    assert ai_therapist._thinking_off(None) == {}


def test_claude_create_sends_the_thinking_off_setting():
    client = MagicMock()
    ai_therapist.claude_create(client, model="claude-sonnet-5-5", max_tokens=10, messages=[])
    assert client.messages.create.call_args.kwargs["thinking"] == {"type": "between_tools"}


class BadRequestError(Exception):
    """Same class name the SDK's 400 error has."""


def test_claude_create_retries_once_without_thinking_if_the_api_refuses_it():
    client = MagicMock()
    client.messages.create.side_effect = [BadRequestError("thinking not accepted"), "ok"]
    assert ai_therapist.claude_create(client, model="claude-haiku-5-5", max_tokens=10) == "ok"
    first, second = client.messages.create.call_args_list
    assert "thinking" in first.kwargs
    assert "thinking" not in second.kwargs


def test_claude_create_does_not_hide_other_errors():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("API down")
    with pytest.raises(RuntimeError):
        ai_therapist.claude_create(client, model="claude-haiku-5-5", max_tokens=10)
    assert client.messages.create.call_count == 1


def test_claude_create_does_not_retry_a_400_when_it_sent_no_thinking_setting():
    client = MagicMock()
    client.messages.create.side_effect = BadRequestError("something else")
    with pytest.raises(BadRequestError):
        ai_therapist.claude_create(client, model="claude-opus-5-5", max_tokens=10)
    assert client.messages.create.call_count == 1


# --- the callers survive a thinking block coming first ----------------------

def _client_replying(*blocks):
    client = MagicMock()
    client.messages.create.return_value = _response(*blocks)
    return client


def test_co_pilot_cards_survive_a_thinking_block_first():
    raw = json.dumps([{"type": "question", "text": "Ask about sleep.", "confidence": 0.9}])
    client = _client_replying(_thinking(), _text(raw))
    with patch("copilot._get_claude_client", return_value=client):
        cards = copilot.generate_suggestions("Client: I'm exhausted", mode="solo")
    assert cards and cards[0]["text"] == "Ask about sleep."


def test_co_pilot_reply_survives_a_thinking_block_first():
    client = _client_replying(_thinking(), _text("Try a grounding exercise."))
    with patch("copilot._get_claude_client", return_value=client):
        reply = copilot.answer_therapist("What could I try next?", transcript="Client: anxious")
    assert reply == "Try a grounding exercise."


def test_the_summary_call_survives_a_thinking_block_first():
    client = _client_replying(_thinking(), _text("A clinical recap."))
    with patch("clinical_summary._get_claude_client", return_value=client):
        assert clinical_summary._call("sys", "user", label="t") == "A clinical recap."


# --- the short EHR chart summary --------------------------------------------

def test_the_ehr_brief_uses_the_fast_model_and_returns_its_text():
    client = _client_replying(_thinking(), _text("The patient discussed stress."))
    with patch("clinical_summary._get_claude_client", return_value=client):
        out = clinical_summary.generate_ehr_brief("Therapist: hi\nClient: stressed", mode="solo")
    assert out == "The patient discussed stress."
    assert client.messages.create.call_args.kwargs["model"] == config.CLAUDE_MODEL_FAST


def test_the_ehr_brief_is_empty_without_a_transcript_and_makes_no_call():
    client = MagicMock()
    with patch("clinical_summary._get_claude_client", return_value=client):
        assert clinical_summary.generate_ehr_brief("   ") == ""
    client.messages.create.assert_not_called()


def test_the_ehr_brief_is_empty_not_an_error_when_the_call_fails():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("API down")
    with patch("clinical_summary._get_claude_client", return_value=client):
        assert clinical_summary.generate_ehr_brief("Client: hi") == ""


# --- the tone and content rules of the chart summary ------------------------

def test_the_chart_summary_prompt_keeps_every_topic_and_leaves_judgment_to_the_clinician():
    p = clinical_summary._EHR_BRIEF_SYSTEM
    assert "Be specific" in p
    assert "Never leave a topic out" in p
    assert "judgment belongs to the clinician" in p


def test_the_chart_summary_prompt_forbids_judging_anyone_but_welcomes_positive_judgments():
    p = clinical_summary._EHR_BRIEF_SYSTEM
    assert "Positive judgments are welcome; negative ones are not." in p
    assert "Never criticize, doubt, or judge" in p
    for banned in ("unrelated", "irrelevant", "off-topic", "poor", "wasted"):
        assert banned in p      # named as words NOT to use


def test_the_chart_summary_prompt_opens_warmly_and_uses_positive_or_neutral_verbs():
    p = clinical_summary._EHR_BRIEF_SYSTEM
    assert "Open with a warm, positive framing" in p
    assert "the clinician shared" in p
    for avoided in ('"almost entirely"', '"mostly"', '"only"', '"just"', '"merely"', '"reading"'):
        assert avoided in p     # named as wording NOT to use


def test_the_chart_summary_prompt_states_what_was_not_discussed_once_without_judgment():
    p = clinical_summary._EHR_BRIEF_SYSTEM
    assert "say so once, as a plain fact, without judgment words" in p


def test_the_chart_summary_prompt_leaves_room_for_every_topic():
    assert "up to about 200 words" in clinical_summary._EHR_BRIEF_SYSTEM


def test_the_chart_summary_prompt_asks_for_the_essentials():
    p = clinical_summary._EHR_BRIEF_SYSTEM
    assert "risk or safety signal" in p
    assert "next steps" in p
    assert "group session" in p


def test_the_chart_summary_prompt_has_no_intent_rule_and_no_unclear_speaker_rule():
    """Both were removed on request (2026-10-25). Pinned so they do not creep back."""
    p = clinical_summary._EHR_BRIEF_SYSTEM.lower()
    assert "intent" not in p
    assert "never why" not in p
    assert "cannot tell who said" not in p
    assert "unclear" not in p


def test_the_chart_summary_prompt_does_not_ask_for_a_little_clinical_content_remark():
    p = clinical_summary._EHR_BRIEF_SYSTEM.lower()
    assert "little clinical content" not in p
    assert "enough clinical content" not in p


def test_the_model_is_told_not_to_write_the_review_reminder_because_one_is_added():
    assert "Do not add a closing review reminder" in clinical_summary._EHR_BRIEF_SYSTEM
    assert clinical_summary.EHR_REVIEW_LINE == "The transcript should be reviewed for accuracy before filing."
