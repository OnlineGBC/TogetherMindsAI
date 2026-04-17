"""
test_startup.py
---------------
Regression tests for module-level startup code in TogetherMindsAI.py.
"""
import pathlib
import re

APP = pathlib.Path(__file__).parent.parent / "TogetherMindsAI.py"


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
