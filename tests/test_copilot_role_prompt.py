"""
tests/test_copilot_role_prompt.py
---------------------------------
The co-pilot must be told WHO it is advising.

Before this, the prompt said "Assume the therapist is trained (CBT, ACT, IFS,
person-centred)" for everybody. A hypnotherapist working on smoking cessation was
therefore offered person-centred moves — "invite them to say more" — twice over,
mid-induction. The only thing the role changed was whether ICD codes were allowed.

These also hold the line for roles that do not exist yet: every role in the table
must say how the co-pilot should think about it, and the prompts must never be
patronising or repeat themselves.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-copilot-role")

import copilot
import roles


# ---------------------------------------------------------------------------
# Every role, including ones added later
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(roles.ROLES))
def test_every_role_says_how_the_copilot_should_think_about_it(role):
    """A new role that forgets this would silently fall back to the
    talking-therapy wording — the exact bug this replaced."""
    framing = roles.copilot_framing(role)
    assert framing["trained_in"].strip()
    assert framing["fits"].strip()


def test_a_role_missing_its_framing_raises_rather_than_guessing(monkeypatch):
    """Loud beats wrong: mis-advising a practitioner mid-session is worse than
    an error the tests catch."""
    monkeypatch.setitem(roles.ROLES, "future_role",
                        {"label": "x", "blurb": "x", "free": set(), "paid": set(),
                         "licence_check": False, "clinical": False})
    with pytest.raises(KeyError):
        roles.copilot_framing("future_role")


def test_the_roles_are_actually_described_differently():
    """If two roles read the same, the role is not doing any work."""
    described = {r: roles.copilot_framing(r)["trained_in"] for r in roles.ROLES}
    assert len(set(described.values())) == len(described)


def test_a_hypnotherapist_is_not_described_as_a_talking_therapist():
    hypno = roles.copilot_framing(roles.HYPNOTHERAPIST)
    blob = (hypno["trained_in"] + " " + hypno["fits"]).lower()
    assert "hypno" in blob
    for school in ("cbt", "act", "ifs", "person-centred"):
        assert school not in blob


# ---------------------------------------------------------------------------
# What actually reaches the model
# ---------------------------------------------------------------------------

def _system_prompt_from(call):
    return call.kwargs["system"]


def test_the_suggestion_prompt_carries_the_role(monkeypatch):
    fake = MagicMock()
    fake.messages.create.return_value.content = [MagicMock(text="[]")]
    with patch.object(copilot, "_get_claude_client", return_value=fake), \
         patch.object(copilot, "retrieve", return_value=[]), \
         patch.object(copilot, "format_reference_block", return_value=""):
        copilot.generate_suggestions("Client: I want to stop smoking.",
                                     role=roles.HYPNOTHERAPIST)
    prompt = _system_prompt_from(fake.messages.create.call_args)
    assert "hypnotherapy" in prompt.lower()
    assert "person-centred" not in prompt.lower()


def test_the_reply_prompt_carries_the_role():
    fake = MagicMock()
    fake.messages.create.return_value.content = [MagicMock(text="ok")]
    with patch.object(copilot, "_get_claude_client", return_value=fake):
        copilot.answer_therapist("What next?", role=roles.HYPNOTHERAPIST)
    prompt = _system_prompt_from(fake.messages.create.call_args)
    assert "hypnotherapy" in prompt.lower()
    assert "person-centred" not in prompt.lower()


def test_a_psychotherapist_still_gets_the_talking_therapy_framing():
    fake = MagicMock()
    fake.messages.create.return_value.content = [MagicMock(text="ok")]
    with patch.object(copilot, "_get_claude_client", return_value=fake):
        copilot.answer_therapist("What next?", role=roles.PSYCHOTHERAPIST)
    assert "person-centred" in _system_prompt_from(fake.messages.create.call_args).lower()


def test_an_unknown_or_missing_role_falls_back_to_the_default():
    """Callers that predate the role argument must keep working."""
    fake = MagicMock()
    fake.messages.create.return_value.content = [MagicMock(text="ok")]
    for role in (None, "not-a-role"):
        with patch.object(copilot, "_get_claude_client", return_value=fake):
            copilot.answer_therapist("What next?", role=role)
        prompt = _system_prompt_from(fake.messages.create.call_args)
        assert roles.copilot_framing(roles.DEFAULT_ROLE)["trained_in"] in prompt


# ---------------------------------------------------------------------------
# Tone and repetition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [copilot.ADVISOR_SYSTEM_PROMPT,
                                    copilot.ADVISOR_REPLY_SYSTEM_PROMPT])
def test_both_prompts_forbid_a_patronising_tone(prompt):
    lowered = prompt.lower()
    assert "patronising" in lowered
    assert "trained and experienced" in lowered


def test_the_suggestion_prompt_forbids_two_cards_saying_the_same_thing():
    """The panel showed two cards that both said "ask them to say more"."""
    assert "NO TWO CARDS MAY MAKE THE SAME SUGGESTION" in copilot.ADVISOR_SYSTEM_PROMPT
