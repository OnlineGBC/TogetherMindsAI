"""Tests that the font files required by the PDF transcript route are present.

fpdf2 opens font files directly from the filesystem via add_font(fname=...).
If the files are missing (e.g. excluded from the Docker build context via
.gcloudignore), the PDF route raises FileNotFoundError and returns a 500.
"""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import TogetherMindsAI as app_module


def test_font_regular_exists():
    path = pathlib.Path(app_module._FONT_REGULAR)
    assert path.exists(), (
        f"DejaVuSans regular font not found at {path}. "
        "Ensure static/fonts/ is NOT excluded in .gcloudignore."
    )


def test_font_bold_exists():
    path = pathlib.Path(app_module._FONT_BOLD)
    assert path.exists(), (
        f"DejaVuSans bold font not found at {path}. "
        "Ensure static/fonts/ is NOT excluded in .gcloudignore."
    )
