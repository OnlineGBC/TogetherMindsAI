"""
tests/test_reading_level.py
---------------------------
Every therapist-facing AI prompt must ask for 8th-grade reading level.

What this can and cannot prove: it checks the INSTRUCTION is in the prompt. It
cannot check the model obeys it, because the wording differs on every call. The
real check is a clinician reading live cards. This test exists so the instruction
cannot be dropped by accident during a future prompt edit.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ["TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-reading")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5001")

import pytest

import clinical_summary
import copilot


# Every prompt the therapist reads the output of.
THERAPIST_FACING = {
    "copilot suggestion cards": copilot.ADVISOR_SYSTEM_PROMPT,
    "copilot private answer": copilot.ADVISOR_REPLY_SYSTEM_PROMPT,
    "clinical recap": clinical_summary._CLINICAL_SYSTEM,
    "ICD/billing note": clinical_summary._CODES_SYSTEM,
}


@pytest.mark.parametrize("label", sorted(THERAPIST_FACING))
def test_prompt_asks_for_8th_grade_reading_level(label):
    prompt = THERAPIST_FACING[label]
    assert "8th-grade reading level" in prompt, f"{label} prompt lost the reading level rule"
    assert "15-20 words" in prompt, f"{label} prompt lost the sentence-length guide"


@pytest.mark.parametrize("label", sorted(THERAPIST_FACING))
def test_prompt_keeps_clinical_terms_with_a_plain_explanation(label):
    """Banning clinical terms outright would cost precision. The rule is: keep the
    exact word, then explain it in a few plain words."""
    prompt = THERAPIST_FACING[label]
    assert "plain words" in prompt, f"{label} prompt lost the plain-explanation rule"


def test_client_recap_is_left_alone():
    """The client-facing recap was already written in everyday language with no
    jargon. It is a different audience and deliberately not part of this change."""
    prompt = clinical_summary._CLIENT_RECAP_SYSTEM
    assert "no clinical" in prompt and "jargon" in prompt
    assert "8th-grade reading level" not in prompt
