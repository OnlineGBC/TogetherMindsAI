"""
Unit tests for session_id.py — the single source of truth for session ID logic.

All sessions (solo, couple, group) now use the same long randomized
private key format from SESSION_CHARSET. These tests verify every
public function's contract.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from session_id import (
    SESSION_CHARSET,
    SESSION_ID_LENGTH,
    generate_session_id,
    is_valid_session_id,
    normalise_join_input,
    rejoin_format_hint,
    rejoin_placeholder,
    _example_session_id,
)


# ---------------------------------------------------------------------------
# Charset sanity
# ---------------------------------------------------------------------------

class TestCharset:
    def test_excludes_uppercase_I(self):
        assert "I" not in SESSION_CHARSET

    def test_excludes_uppercase_O(self):
        assert "O" not in SESSION_CHARSET

    def test_excludes_lowercase_i(self):
        assert "i" not in SESSION_CHARSET

    def test_excludes_lowercase_l(self):
        assert "l" not in SESSION_CHARSET

    def test_excludes_lowercase_o(self):
        assert "o" not in SESSION_CHARSET

    def test_excludes_digit_zero(self):
        assert "0" not in SESSION_CHARSET

    def test_excludes_digit_one(self):
        assert "1" not in SESSION_CHARSET

    def test_contains_uppercase_letters(self):
        assert "A" in SESSION_CHARSET and "Z" in SESSION_CHARSET

    def test_contains_lowercase_letters(self):
        assert "a" in SESSION_CHARSET and "z" in SESSION_CHARSET

    def test_contains_digits(self):
        assert "2" in SESSION_CHARSET and "9" in SESSION_CHARSET

    def test_charset_length(self):
        # 24 upper (A-Z minus I,O) + 23 lower (a-z minus i,l,o) + 8 digits (2-9) + 4 symbols (-_!$)
        assert len(SESSION_CHARSET) == 59, (
            f"Expected 59 chars (24 upper + 23 lower + 8 digits + 4 symbols), got {len(SESSION_CHARSET)}"
        )

    def test_no_duplicates(self):
        assert len(SESSION_CHARSET) == len(set(SESSION_CHARSET))


# ---------------------------------------------------------------------------
# generate_session_id
# ---------------------------------------------------------------------------

class TestGenerateSessionId:
    def test_returns_correct_length(self):
        assert len(generate_session_id()) == SESSION_ID_LENGTH

    def test_all_chars_in_charset(self):
        for _ in range(50):
            result = generate_session_id()
            for ch in result:
                assert ch in SESSION_CHARSET, f"Character {ch!r} not in SESSION_CHARSET"

    def test_is_mixed_case(self):
        """Over 50 IDs, should see both upper and lowercase characters."""
        ids = [generate_session_id() for _ in range(50)]
        all_chars = "".join(ids)
        has_upper = any(c.isupper() for c in all_chars)
        has_lower = any(c.islower() for c in all_chars)
        assert has_upper and has_lower, "Expected mixed case across 50 generated IDs"

    def test_two_calls_differ(self):
        ids = {generate_session_id() for _ in range(20)}
        assert len(ids) > 1, "20 consecutive IDs were all identical"

    def test_never_contains_ambiguous_chars(self):
        for _ in range(100):
            result = generate_session_id()
            for forbidden in ("I", "O", "i", "l", "o", "0", "1"):
                assert forbidden not in result, (
                    f"Ambiguous character {forbidden!r} found in {result!r}"
                )

    def test_collision_avoidance(self):
        existing = {generate_session_id() for _ in range(50)}
        result = generate_session_id(existing_ids=existing)
        assert len(result) == SESSION_ID_LENGTH

    def test_raises_if_all_candidates_collide(self, monkeypatch):
        monkeypatch.setattr("session_id.secrets.choice", lambda _: "A")
        # Every candidate is "A" * SESSION_ID_LENGTH, which already exists → no unique id.
        with pytest.raises(RuntimeError, match="unique session ID"):
            generate_session_id(existing_ids={"A" * SESSION_ID_LENGTH})


# ---------------------------------------------------------------------------
# is_valid_session_id
# ---------------------------------------------------------------------------

class TestIsValidSessionId:
    def test_true_for_generated_id(self):
        for _ in range(20):
            assert is_valid_session_id(generate_session_id())

    def test_false_for_full_uuid(self):
        assert not is_valid_session_id("7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7")

    def test_false_for_too_short(self):
        assert not is_valid_session_id("aB3k7")

    def test_false_for_too_long(self):
        assert not is_valid_session_id("aB3k7MX")

    def test_false_for_excluded_uppercase_I(self):
        assert not is_valid_session_id("ABCDEI")

    def test_false_for_excluded_uppercase_O(self):
        assert not is_valid_session_id("ABCDEO")

    def test_false_for_excluded_lowercase_i(self):
        assert not is_valid_session_id("abcdei")

    def test_false_for_excluded_lowercase_l(self):
        assert not is_valid_session_id("abcdel")

    def test_false_for_excluded_lowercase_o(self):
        assert not is_valid_session_id("abcdeo")

    def test_false_for_excluded_digit_zero(self):
        assert not is_valid_session_id("aB3k70")

    def test_false_for_excluded_digit_one(self):
        assert not is_valid_session_id("aB3k71")

    def test_validates_stored_form_exactly(self):
        """is_valid_session_id checks the stored form (charset membership).
        Lookups are case-insensitive, but validation checks the raw stored value."""
        # A generated ID must always pass
        assert is_valid_session_id(generate_session_id())

    def test_true_for_example_id(self):
        assert is_valid_session_id(_example_session_id())


# ---------------------------------------------------------------------------
# normalise_join_input
# ---------------------------------------------------------------------------

class TestNormaliseJoinInput:
    def test_strips_whitespace(self):
        assert normalise_join_input("  aB3k7M  ") == "AB3K7M"

    def test_uppercases_input(self):
        """Session IDs are case-insensitive — normalisation uppercases the input."""
        assert normalise_join_input("aB3k7M") == "AB3K7M"
        assert normalise_join_input("ab3k7m") == "AB3K7M"

    def test_already_uppercase_is_unchanged(self):
        assert normalise_join_input("AB3K7M") == "AB3K7M"

    def test_no_op_on_already_normalised(self):
        sid = generate_session_id()
        assert normalise_join_input(sid) == normalise_join_input(normalise_join_input(sid))


# ---------------------------------------------------------------------------
# rejoin_format_hint
# ---------------------------------------------------------------------------

class TestRejoinFormatHint:
    def test_returns_non_empty(self):
        assert rejoin_format_hint().strip()

    def test_mentions_session_id_length(self):
        assert str(SESSION_ID_LENGTH) in rejoin_format_hint()

    def test_mentions_case_insensitive(self):
        assert "not case-sensitive" in rejoin_format_hint().lower()

    def test_does_not_mention_old_4_digit(self):
        assert "4-digit" not in rejoin_format_hint()

    def test_does_not_mention_uuid(self):
        assert "UUID" not in rejoin_format_hint() and "uuid" not in rejoin_format_hint()

    def test_contains_example(self):
        assert _example_session_id() in rejoin_format_hint()


# ---------------------------------------------------------------------------
# rejoin_placeholder
# ---------------------------------------------------------------------------

class TestRejoinPlaceholder:
    def test_returns_non_empty(self):
        assert rejoin_placeholder().strip()

    def test_contains_example(self):
        assert _example_session_id() in rejoin_placeholder()

    def test_does_not_contain_1234(self):
        assert "1234" not in rejoin_placeholder()
