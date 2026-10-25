"""
tests/test_green_buttons.py
---------------------------
Primary buttons are green, not Bootstrap's default blue, and the button that starts
an AI summary blinks "Generating . . ." while it works.

These check the files that ship (CSS, templates, page script). How it LOOKS, and the
blinking itself, can only be confirmed by eye in a browser.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
EHR = (ROOT / "templates" / "ehr_result.html").read_text(encoding="utf-8")
GREEN = "#198754"
BLUES = ("#0d6efd", "#0b5ed7", "#0a58ca", "#007bff", "#0069d9")


def _rule(css, selector):
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, selector + " rule not found"
    return m.group(1)


# --- the whole app -----------------------------------------------------------

def test_bootstrap_primary_buttons_are_green_everywhere():
    body = _rule(CSS, ".btn-primary")
    assert "--bs-btn-bg: " + GREEN in body
    assert "--bs-btn-border-color: " + GREEN in body
    assert "--bs-btn-hover-bg:" in body and "--bs-btn-active-bg:" in body
    assert "--bs-btn-disabled-bg: " + GREEN in body


def test_bootstrap_outline_primary_buttons_are_green_too():
    body = _rule(CSS, ".btn-outline-primary")
    assert "--bs-btn-color: " + GREEN in body
    assert "--bs-btn-border-color: " + GREEN in body
    assert "--bs-btn-hover-bg: " + GREEN in body


def test_the_stylesheet_has_no_blue_button_colours_left():
    for blue in BLUES:
        assert blue not in CSS.lower()


def test_every_page_shell_loads_the_new_stylesheet_version():
    """A changed stylesheet needs a new ?v= token or browsers keep the old one."""
    for name in ("base.html", "base_embed.html"):
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "css/style.css') }}?v=2026-10-25-a" in html, name


# --- the EHR launch page -----------------------------------------------------

def test_the_ehr_page_buttons_are_green_with_no_blue_left():
    assert "background: " + GREEN in EHR
    for blue in BLUES:
        assert blue not in EHR.lower()


def test_the_quiet_refresh_button_stays_grey():
    assert re.search(r"\.btn-quiet\s*\{\s*background:\s*#6c757d", EHR)


def test_load_recap_is_marked_as_an_ai_action():
    m = re.search(r"<button[^>]*data-generates[^>]*>\s*Load recap", EHR)
    assert m, "the Load recap button must carry data-generates"


def test_the_generating_button_blinks_and_respects_reduced_motion():
    assert "@keyframes tm-blink" in EHR
    assert re.search(r"\.btn-generating\s*\{[^}]*animation:\s*tm-blink", EHR)
    assert "prefers-reduced-motion: reduce" in EHR
    assert re.search(r"prefers-reduced-motion: reduce\)\s*\{[^}]*animation:\s*none", EHR)


def test_pressing_load_recap_shows_generating_and_ignores_a_second_press():
    assert 'textContent = "Generating . . ."' in EHR
    assert "e.submitter" in EHR
    assert 'hasAttribute("data-generates")' in EHR
    assert "e.preventDefault()" in EHR            # second press is ignored
    assert 'setAttribute("aria-busy", "true")' in EHR


def test_going_back_to_the_page_does_not_leave_a_stuck_button():
    assert '"pageshow"' in EHR and "e.persisted" in EHR
