"""
test_startup.py
---------------
Regression tests for module-level startup code in TogetherMindsAI.py.
"""
import pathlib
import re

APP = pathlib.Path(__file__).parent.parent / "TogetherMindsAI.py"


class TestWarmupAlwaysRuns:

    def test_no_reloader_parent_guard_on_warm_up(self):
        """Regression: the warm-up must NOT be gated on WERKZEUG_RUN_MAIN.
        When use_reloader=False (local dev) and FLASK_DEBUG=true, WERKZEUG_RUN_MAIN
        is never set, so the old '_is_reloader_parent' check always evaluated True
        and silently skipped the model warm-up, causing a delay on the first request."""
        source = APP.read_text()
        assert "_is_reloader_parent" not in source, (
            "'_is_reloader_parent' guard found — warm-up would be skipped when "
            "FLASK_DEBUG=true and use_reloader=False (local dev)"
        )


class TestThreadingImport:

    def test_import_threading_present_before_thread_usage(self):
        """Regression: 'import threading' must appear before threading.Thread() in the
        startup block.  When it was accidentally removed, the daemon thread that purges
        expired sessions raised NameError: name 'threading' is not defined at startup."""
        source = APP.read_text()
        import_pos = source.find("import threading")
        thread_usage_pos = source.find("threading.Thread(")
        assert import_pos != -1, "'import threading' not found in TogetherMindsAI.py"
        assert thread_usage_pos != -1, "'threading.Thread(' not found in TogetherMindsAI.py"
        assert import_pos < thread_usage_pos, (
            "'import threading' must appear before 'threading.Thread(' in the startup block"
        )
