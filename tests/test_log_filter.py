"""
tests/test_log_filter.py
------------------------
Tests for the SensitiveDataFilter log filter (finding 4.3).

Verifies that:
  - JSON-style "text": "..." values are redacted
  - "content", "body", "prompt", "response" values are redacted
  - Short values (≤ 8 chars) are left as-is (they are metadata, not content)
  - Messages with no sensitive fields pass through unchanged
  - Python repr-style key='value' patterns are redacted
  - The filter is installed on the root logger at startup
  - Exception objects logged via %s args are scrubbed before emission
"""

import logging
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from log_filter import SensitiveDataFilter, install_log_filter, _scrub


# ---------------------------------------------------------------------------
# _scrub unit tests — exercises the regex directly
# ---------------------------------------------------------------------------

class TestScrub:

    def test_redacts_json_text_field(self):
        msg = '{"text": "I feel really hopeless and alone today"}'
        result = _scrub(msg)
        assert "hopeless" not in result
        assert '"text": "[REDACTED]"' in result

    def test_redacts_json_content_field(self):
        msg = '{"content": "Please help me, I cannot cope anymore"}'
        result = _scrub(msg)
        assert "cannot cope" not in result
        assert "[REDACTED]" in result

    def test_redacts_json_prompt_field(self):
        msg = '{"prompt": "Tell me about my suicidal thoughts"}'
        result = _scrub(msg)
        assert "suicidal" not in result
        assert "[REDACTED]" in result

    def test_redacts_json_response_field(self):
        msg = '{"response": "I hear you, this sounds very difficult"}'
        result = _scrub(msg)
        assert "very difficult" not in result
        assert "[REDACTED]" in result

    def test_redacts_json_body_field(self):
        msg = '{"body": "A long message body with personal details inside"}'
        result = _scrub(msg)
        assert "personal details" not in result
        assert "[REDACTED]" in result

    def test_redacts_json_message_field(self):
        msg = '{"message": "A confidential note that must be redacted from logs"}'
        result = _scrub(msg)
        assert "confidential note" not in result
        assert "[REDACTED]" in result

    def test_redacts_json_transcript_field(self):
        msg = '{"transcript": "The client described a difficult week at length"}'
        result = _scrub(msg)
        assert "difficult week" not in result
        assert "[REDACTED]" in result

    def test_short_values_not_redacted(self):
        # Values ≤ 8 chars are metadata (mode names, IDs) — must not be redacted
        msg = '{"text": "solo"}'
        result = _scrub(msg)
        assert result == msg

    def test_non_sensitive_fields_unchanged(self):
        msg = '{"mode": "couple", "session_id": "aBcDeF", "message_length": 42}'
        assert _scrub(msg) == msg

    def test_plain_text_no_sensitive_fields_unchanged(self):
        msg = "Purged 3 expired sessions."
        assert _scrub(msg) == msg

    def test_redacts_repr_style_text_kwarg(self):
        # SQLAlchemy errors can include  text='<encrypted blob>'
        msg = "IntegrityError: text='gAAAABsomeLongEncryptedValue12345'"
        result = _scrub(msg)
        assert "gAAAAB" not in result
        assert "[REDACTED]" in result

    def test_case_insensitive_field_names(self):
        msg = '{"TEXT": "this is a long sensitive message value here"}'
        result = _scrub(msg)
        assert "sensitive message" not in result
        assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# SensitiveDataFilter — LogRecord-level tests
# ---------------------------------------------------------------------------

class TestSensitiveDataFilter:

    def _make_record(self, msg, *args):
        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_filter_redacts_sensitive_msg(self):
        f = SensitiveDataFilter()
        record = self._make_record('{"text": "I am feeling very anxious and scared"}')
        f.filter(record)
        assert "anxious" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_filter_collapses_args_before_scrubbing(self):
        """Args are formatted into msg so the scrubber sees the full string."""
        f = SensitiveDataFilter()
        record = self._make_record(
            'Claude error for request: %s',
            '{"content": "A long sensitive patient message that must be redacted"}',
        )
        f.filter(record)
        assert "sensitive patient" not in record.msg
        assert record.args is None   # args consumed

    def test_filter_passes_clean_record_unchanged(self):
        f = SensitiveDataFilter()
        record = self._make_record("Purged %d expired sessions.", 5)
        f.filter(record)
        assert "Purged 5 expired sessions." == record.msg

    def test_filter_always_returns_true(self):
        """Filter never suppresses records — it only scrubs them."""
        f = SensitiveDataFilter()
        record = self._make_record('{"text": "sensitive content that is long enough"}')
        assert f.filter(record) is True


# ---------------------------------------------------------------------------
# install_log_filter — integration check
# ---------------------------------------------------------------------------

class TestInstallLogFilter:

    def test_filter_installed_on_root_logger(self):
        install_log_filter()
        root = logging.getLogger()
        assert any(isinstance(f, SensitiveDataFilter) for f in root.filters)

    def test_install_is_idempotent(self):
        """Calling install_log_filter() twice must not add duplicate filters."""
        install_log_filter()
        install_log_filter()
        root = logging.getLogger()
        count = sum(1 for f in root.filters if isinstance(f, SensitiveDataFilter))
        assert count == 1
