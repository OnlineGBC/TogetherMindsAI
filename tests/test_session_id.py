"""
Unit tests for session_id.py — the single source of truth for session ID logic.

These tests are pure (no Flask context required) and verify the contracts of
every public function in the module.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from session_id import (
    GROUP_CHARSET,
    GROUP_ID_LENGTH,
    DISPLAY_ID_LENGTH,
    generate_group_session_id,
    to_display_id,
    is_valid_group_id,
    is_display_id,
    normalise_join_input,
    rejoin_format_hint,
    rejoin_placeholder,
    _example_group_id,
)


# ---------------------------------------------------------------------------
# generate_group_session_id
# ---------------------------------------------------------------------------

class TestGenerateGroupSessionId:
    def test_returns_correct_length(self):
        result = generate_group_session_id()
        assert len(result) == GROUP_ID_LENGTH

    def test_all_chars_in_charset(self):
        for _ in range(20):
            result = generate_group_session_id()
            for ch in result:
                assert ch in GROUP_CHARSET, f"Character {ch!r} not in GROUP_CHARSET"

    def test_returns_uppercase(self):
        result = generate_group_session_id()
        assert result == result.upper()

    def test_two_calls_differ(self):
        """Statistically should differ — probability of collision is 1/32^6 ≈ 10^-9."""
        ids = {generate_group_session_id() for _ in range(20)}
        assert len(ids) > 1, "20 consecutive IDs were all identical — generator is broken"

    def test_excludes_ambiguous_characters(self):
        """I, O, 0, 1 must never appear (too easily confused)."""
        for _ in range(100):
            result = generate_group_session_id()
            for forbidden in ("I", "O", "0", "1"):
                assert forbidden not in result, (
                    f"Ambiguous character {forbidden!r} found in generated ID {result!r}"
                )

    def test_collision_avoidance_with_existing_ids(self):
        """When all but one candidate would collide, eventually returns the free one."""
        # Generate one real ID and treat every other possible value as "existing"
        # This is impractical for 32^6 IDs, so instead: restrict by patching.
        # Simpler approach: pass a set containing many generated IDs and verify we
        # still get something back (not RuntimeError) at small scale.
        existing = {generate_group_session_id() for _ in range(50)}
        # Should still succeed (50 collisions out of 1B possibilities is trivial)
        result = generate_group_session_id(existing_ids=existing)
        assert len(result) == GROUP_ID_LENGTH

    def test_raises_if_all_candidates_collide(self, monkeypatch):
        """If every candidate is in existing_ids, RuntimeError is raised."""
        # Force the generator to always produce "AAAAAA"
        monkeypatch.setattr("session_id.secrets.choice", lambda _: "A")
        with pytest.raises(RuntimeError, match="unique group session ID"):
            generate_group_session_id(existing_ids={"AAAAAA"})


# ---------------------------------------------------------------------------
# to_display_id
# ---------------------------------------------------------------------------

class TestToDisplayId:
    KNOWN_UUID = "7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7"
    KNOWN_DISPLAY = "7A6D1E"  # hyphens stripped, uppercased, first 6 chars

    def test_solo_strips_hyphens_and_uppercases(self):
        assert to_display_id(self.KNOWN_UUID, "solo") == self.KNOWN_DISPLAY

    def test_couple_same_as_solo(self):
        assert to_display_id(self.KNOWN_UUID, "couple") == self.KNOWN_DISPLAY

    def test_group_returns_id_unchanged(self):
        group_id = "AB3K7M"
        assert to_display_id(group_id, "group") == group_id

    def test_solo_result_is_exactly_display_id_length(self):
        result = to_display_id(self.KNOWN_UUID, "solo")
        assert len(result) == DISPLAY_ID_LENGTH

    def test_couple_result_is_exactly_display_id_length(self):
        result = to_display_id(self.KNOWN_UUID, "couple")
        assert len(result) == DISPLAY_ID_LENGTH

    def test_solo_result_never_contains_hyphen(self):
        result = to_display_id(self.KNOWN_UUID, "solo")
        assert "-" not in result

    def test_couple_result_never_contains_hyphen(self):
        result = to_display_id(self.KNOWN_UUID, "couple")
        assert "-" not in result

    def test_solo_result_is_uppercase(self):
        result = to_display_id(self.KNOWN_UUID, "solo")
        assert result == result.upper()

    def test_couple_result_is_uppercase(self):
        result = to_display_id(self.KNOWN_UUID, "couple")
        assert result == result.upper()

    def test_raises_for_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown session mode"):
            to_display_id(self.KNOWN_UUID, "family")

    def test_raises_for_empty_mode(self):
        with pytest.raises(ValueError):
            to_display_id(self.KNOWN_UUID, "")

    def test_group_display_equals_internal(self):
        """For group sessions, display ID must equal the internal ID exactly."""
        group_id = generate_group_session_id()
        assert to_display_id(group_id, "group") == group_id


# ---------------------------------------------------------------------------
# is_valid_group_id
# ---------------------------------------------------------------------------

class TestIsValidGroupId:
    def test_true_for_generated_id(self):
        for _ in range(20):
            assert is_valid_group_id(generate_group_session_id())

    def test_false_for_full_uuid(self):
        assert not is_valid_group_id("7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7")

    def test_false_for_too_short(self):
        assert not is_valid_group_id("AB3K7")   # 5 chars

    def test_false_for_too_long(self):
        assert not is_valid_group_id("AB3K7MX")  # 7 chars

    def test_false_for_excluded_char_O(self):
        assert not is_valid_group_id("ABCDO2")   # O is excluded

    def test_false_for_excluded_char_I(self):
        assert not is_valid_group_id("ABCDI2")   # I is excluded

    def test_false_for_excluded_char_zero(self):
        assert not is_valid_group_id("ABC002")   # 0 is excluded

    def test_false_for_excluded_char_one(self):
        assert not is_valid_group_id("ABC1B2")   # 1 is excluded

    def test_false_for_lowercase(self):
        """Charset is uppercase; lowercase inputs must return False."""
        assert not is_valid_group_id("ab3k7m")

    def test_true_for_example_id(self):
        assert is_valid_group_id(_example_group_id())


# ---------------------------------------------------------------------------
# normalise_join_input
# ---------------------------------------------------------------------------

class TestNormaliseJoinInput:
    def test_uppercases_valid_group_id(self):
        assert normalise_join_input("ab3k7m") == "AB3K7M"

    def test_leaves_uuid_unchanged(self):
        uuid_str = "7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7"
        assert normalise_join_input(uuid_str) == uuid_str

    def test_leaves_nickname_unchanged(self):
        assert normalise_join_input("My Monday session") == "My Monday session"

    def test_uppercase_group_id_unchanged(self):
        group_id = generate_group_session_id()
        assert normalise_join_input(group_id) == group_id


# ---------------------------------------------------------------------------
# is_display_id
# ---------------------------------------------------------------------------

class TestIsDisplayId:
    def test_true_for_6_char_uppercase_alnum(self):
        assert is_display_id("7A6D1E")

    def test_true_for_generated_group_id(self):
        assert is_display_id(generate_group_session_id())

    def test_false_for_full_uuid(self):
        assert not is_display_id("7a6d1ebd-e6b6-4b7d-afbf-9c56984b34f7")

    def test_false_for_lowercase(self):
        assert not is_display_id("7a6d1e")

    def test_false_for_too_short(self):
        assert not is_display_id("7A6D1")

    def test_false_for_too_long(self):
        assert not is_display_id("7A6D1EX")

    def test_false_for_contains_hyphen(self):
        assert not is_display_id("7A6D-E")


# ---------------------------------------------------------------------------
# rejoin_format_hint
# ---------------------------------------------------------------------------

class TestRejoinFormatHint:
    def test_none_mode_contains_group_length(self):
        hint = rejoin_format_hint()
        assert str(GROUP_ID_LENGTH) in hint

    def test_none_mode_does_not_say_4_digit(self):
        """Regression: the old hardcoded text said '4-digit' which was wrong."""
        hint = rejoin_format_hint()
        assert "4-digit" not in hint

    def test_group_mode_does_not_mention_uuid(self):
        hint = rejoin_format_hint("group")
        assert "UUID" not in hint and "uuid" not in hint.lower()

    def test_group_mode_does_not_mention_couples(self):
        hint = rejoin_format_hint("group")
        assert "couple" not in hint.lower()

    def test_solo_mode_returns_non_empty(self):
        assert rejoin_format_hint("solo").strip()

    def test_couple_mode_returns_non_empty(self):
        assert rejoin_format_hint("couple").strip()

    def test_group_mode_returns_non_empty(self):
        assert rejoin_format_hint("group").strip()

    def test_none_mode_returns_non_empty(self):
        assert rejoin_format_hint(None).strip()

    def test_hint_contains_example_id(self):
        """The hint should include a recognisable example from the charset."""
        hint = rejoin_format_hint("group")
        assert _example_group_id() in hint


# ---------------------------------------------------------------------------
# rejoin_placeholder
# ---------------------------------------------------------------------------

class TestRejoinPlaceholder:
    def test_general_placeholder_is_non_empty(self):
        assert rejoin_placeholder().strip()

    def test_group_placeholder_is_non_empty(self):
        assert rejoin_placeholder("group").strip()

    def test_solo_placeholder_is_non_empty(self):
        assert rejoin_placeholder("solo").strip()

    def test_couple_placeholder_is_non_empty(self):
        assert rejoin_placeholder("couple").strip()

    def test_group_placeholder_contains_example(self):
        """Placeholder should show a realistic example group ID."""
        placeholder = rejoin_placeholder("group")
        assert _example_group_id() in placeholder

    def test_general_placeholder_does_not_say_1234(self):
        """Regression: old placeholder said '1234' which implied a 4-digit numeric code."""
        assert "1234" not in rejoin_placeholder()
