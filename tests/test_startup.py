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
        source = APP.read_text(encoding="utf-8")
        assert "_is_reloader_parent" not in source, (
            "'_is_reloader_parent' guard found — warm-up would be skipped when "
            "FLASK_DEBUG=true and use_reloader=False (local dev)"
        )


class TestStartupForwardReferences:
    """The main startup block runs during import (under `not IS_TESTING`), so any
    function it references must be defined ABOVE it. These guard the import-order
    NameError that took the service down: the ICD reminder job was registered in
    the startup block before its function was defined further down the module."""

    def test_icd_reminder_job_registered_after_its_def(self):
        source = APP.read_text(encoding="utf-8")
        def_pos = source.find("def _send_icd_refresh_reminder")
        reg_pos = source.find('id="icd_refresh_reminder"')
        assert def_pos != -1, "_send_icd_refresh_reminder definition not found"
        assert reg_pos != -1, "icd_refresh_reminder job registration not found"
        assert def_pos < reg_pos, (
            "ICD reminder cron job is registered before _send_icd_refresh_reminder "
            "is defined — import-time NameError under gunicorn (not IS_TESTING)"
        )

    def test_icd_catchup_thread_started_after_its_def(self):
        source = APP.read_text(encoding="utf-8")
        def_pos = source.find("def _icd_reminder_catchup")
        use_pos = source.find("threading.Thread(target=_icd_reminder_catchup")
        assert def_pos != -1, "_icd_reminder_catchup definition not found"
        assert use_pos != -1, "_icd_reminder_catchup thread start not found"
        assert def_pos < use_pos, (
            "ICD catch-up thread is started before _icd_reminder_catchup is defined "
            "— import-time NameError under gunicorn (not IS_TESTING)"
        )


class TestThreadingImport:

    def test_import_threading_present_before_thread_usage(self):
        """Regression: 'import threading' must appear before threading.Thread() in the
        startup block.  When it was accidentally removed, the daemon thread that purges
        expired sessions raised NameError: name 'threading' is not defined at startup."""
        source = APP.read_text(encoding="utf-8")
        import_pos = source.find("import threading")
        thread_usage_pos = source.find("threading.Thread(")
        assert import_pos != -1, "'import threading' not found in TogetherMindsAI.py"
        assert thread_usage_pos != -1, "'threading.Thread(' not found in TogetherMindsAI.py"
        assert import_pos < thread_usage_pos, (
            "'import threading' must appear before 'threading.Thread(' in the startup block"
        )
