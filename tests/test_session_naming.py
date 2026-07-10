"""
tests/test_session_naming.py
----------------------------
Safe filename prefixing for a session's downloads (emailed + manual):

* the therapist's chosen friendly name is slugified into a safe filename part,
* falling back to the session id when there's no usable name,
* and the friendly name is validated at input so unsafe characters never reach
  the display label, the rejoin key, or the download header.
"""
import types

from session_id import filename_slug
from TogetherMindsAI import (
    _session_file_prefix,
    _download_name,
    _friendly_name_is_valid,
)


def _ts(friendly, sid="Zkp2Y6qSyDv3sx"):
    return types.SimpleNamespace(friendly_name=friendly, id=sid)


class TestFilenameSlug:
    def test_spaces_become_hyphens(self):
        assert filename_slug("Morning Check-in") == "Morning-Check-in"

    def test_strips_unsafe_chars(self):
        assert filename_slug('Anxiety: wk3 "notes"/2') == "Anxiety-wk3-notes2"

    def test_folds_accents_to_ascii(self):
        assert filename_slug("Café") == "Cafe"

    def test_all_emoji_falls_back(self):
        assert filename_slug("\U0001F33F\U0001F33F", fallback="Zkp2Y6q") == "Zkp2Y6q"

    def test_legacy_id_symbols_dropped_in_fallback(self):
        # A pre-change session id could contain ! and $ — the slug drops them.
        assert filename_slug("", fallback="Z!kp_2Y6q$X") == "Zkp_2Y6qX"

    def test_length_capped(self):
        assert len(filename_slug("a" * 100)) == 40

    def test_never_empty(self):
        assert filename_slug("", fallback="") == "session"


class TestSessionFilePrefix:
    def test_uses_friendly_name_when_set(self):
        assert _session_file_prefix(_ts("Morning Check-in")) == "Morning-Check-in"

    def test_falls_back_to_session_id(self):
        assert _session_file_prefix(_ts(None, sid="Zkp2Y6qSyDv3sx")) == "Zkp2Y6qSyDv3sx"


class TestDownloadName:
    def test_transcript_pdf_carries_prefix(self):
        n = _download_name(_ts("Morning Check-in"), "transcript", "pdf")
        assert n.startswith("Morning-Check-in_transcript_")
        assert n.endswith(".pdf")

    def test_recording_uses_session_id_prefix(self):
        n = _download_name(_ts(None, sid="Zkp2Y6q"), "recording", "mp4")
        assert n.startswith("Zkp2Y6q_recording_")
        assert n.endswith(".mp4")


class TestFriendlyNameValidation:
    def test_accepts_normal_name(self):
        assert _friendly_name_is_valid("Morning Check-in 3")

    def test_accepts_accents_and_punctuation(self):
        assert _friendly_name_is_valid("Café (wk 3) — O'Brien")

    def test_accepts_non_latin(self):
        assert _friendly_name_is_valid("北京 1")   # "北京 1"

    def test_rejects_path_characters(self):
        assert not _friendly_name_is_valid("bad/name")
        assert not _friendly_name_is_valid("a\\b")
        assert not _friendly_name_is_valid('a"b')
        assert not _friendly_name_is_valid("a:b")

    def test_rejects_control_characters(self):
        assert not _friendly_name_is_valid("a\nb")

    def test_rejects_no_alphanumeric(self):
        assert not _friendly_name_is_valid("--- ...")
