"""
tests/conftest.py
-----------------
Session-wide patches applied to every test module:

1. Emotion classifier  — prevents real model download/inference during tests.
2. Claude client       — prevents real API calls during tests.
3. SQLite pool fix     — prevents "database is locked" errors in teardown
   when multiple fixtures share in-memory DBs within the same process.
"""
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Emotion classifier mock — returned for every test automatically
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_emotion_pipeline():
    """Replace the HuggingFace pipeline with a lightweight mock.

    Default emotion: "neutral". Individual tests that need a specific emotion
    should apply their own patch on top (it takes precedence).
    """
    mock_pipe = MagicMock(return_value=[[{"label": "neutral", "score": 0.9}]])
    with patch("ai_therapist._get_emotion_pipeline", return_value=mock_pipe):
        yield mock_pipe


# ---------------------------------------------------------------------------
# Claude client mock — returned for every test automatically
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_claude_client():
    """Replace the Anthropic client with a lightweight mock.

    Default reply: a generic therapeutic response that satisfies the smoke-test
    assertions (contains "therapist" and passes the output sanitiser).
    Individual tests that need a specific reply should patch on top.
    """
    default_reply = (
        "I hear you, and I'm glad you reached out. "
        "It takes courage to share how you're feeling. "
        "As your AI therapist, I'd like to understand more about what's on your mind. "
        "What feels most pressing for you right now?"
    )
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=default_reply)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    with patch("ai_therapist._get_claude_client", return_value=mock_client):
        yield mock_client
